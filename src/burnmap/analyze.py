"""Turn a parsed session into a spend map: totals, waste buckets, tool ranking.

Three waste signals are computed, each with a conservative dollar estimate:

``loops``          The same tool called on the same target 3+ times in a row.
                   Every repeat after the first pair re-sends the whole context,
                   so the extra turns are pure avoidable input cost.
``cache_misses``   Large fresh prompts (>= ``cache_miss_min_tokens``) in a session
                   that otherwise uses prompt caching. Pricing them at the cache
                   read rate is what they should have cost.
``context_creep``  Input size that keeps growing past ``creep_factor`` x the
                   session median and never shrinks. That tail is what a
                   compaction or a narrower context window would have removed.

Estimates are deliberately conservative and always reported next to the exact
assumption that produced them, so a reviewer can argue with the number instead
of guessing where it came from.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser import Turn
from .pricing import PriceBook, Rate

__all__ = ["WasteItem", "SpendMap", "analyze_session", "LOOP_REPEAT_THRESHOLD"]

LOOP_REPEAT_THRESHOLD = 3  # 3+ identical consecutive calls counts as a loop
CACHE_MISS_MIN_TOKENS = 2_000
CREEP_FACTOR = 1.8
CREEP_MIN_GROWTH = 2_000


@dataclass
class WasteItem:
    kind: str
    headline: str
    turns: List[int]
    tokens_wasted: int
    cost_usd: float
    fix: str
    # Per-turn USD actually attributable to this rule. Used to de-duplicate
    # across rules so the same dollar is never counted twice.
    _amounts: Dict[int, float] = field(default_factory=dict, repr=False)


@dataclass
class ToolCost:
    name: str
    calls: int
    tokens: int
    cost_usd: float
    turns: int = 0

    @property
    def tokens_per_call(self) -> float:
        return self.tokens / self.calls if self.calls else 0.0


@dataclass
class SpendMap:
    turns: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    models: Dict[str, Dict[str, float]] = field(default_factory=dict)
    tools: List[ToolCost] = field(default_factory=list)
    waste: List[WasteItem] = field(default_factory=list)
    unknown_models: List[str] = field(default_factory=list)

    @property
    def cache_hit_rate(self) -> float:
        """Share of input-side tokens that were served from cache."""
        denom = self.cache_read_tokens + self.input_tokens
        return self.cache_read_tokens / denom if denom else 0.0

    @property
    def waste_cost_usd(self) -> float:
        """Estimated avoidable spend.

        Rules overlap (a loop turn can also be a cache miss), so per-turn
        amounts are merged by taking the largest single attribution for each
        turn instead of summing. That keeps the total honest: it can never
        exceed what the session actually cost.
        """
        worst: Dict[int, float] = {}
        for item in self.waste:
            for turn_index, amount in item._amounts.items():
                if amount > worst.get(turn_index, 0.0):
                    worst[turn_index] = amount
        return min(sum(worst.values()), self.total_cost_usd)

    @property
    def waste_share(self) -> float:
        return self.waste_cost_usd / self.total_cost_usd if self.total_cost_usd else 0.0

    @property
    def cost_per_turn(self) -> float:
        return self.total_cost_usd / self.turns if self.turns else 0.0


def _input_cost(book: PriceBook, turn: Turn, tokens: Optional[float] = None) -> float:
    """Cost of a turn's input side only, optionally for a subset of tokens."""
    rate = book.rate_for(turn.model)
    if tokens is None:
        return (
            turn.input_tokens / 1_000_000 * rate.input
            + turn.cache_read_tokens / 1_000_000 * rate.cache_read
            + turn.cache_write_tokens / 1_000_000 * rate.cache_write
        )
    return tokens / 1_000_000 * rate.input


def _find_loops(
    turns: List[Turn], book: PriceBook, threshold: int = LOOP_REPEAT_THRESHOLD
) -> List[WasteItem]:
    """Consecutive identical tool calls. Every repeat re-sends the whole context."""
    groups: List[List[Turn]] = []
    current: List[Turn] = []
    for turn in turns:
        if turn.has_tools and turn.tool_signature:
            if current and turn.tool_signature == current[-1].tool_signature:
                current.append(turn)
                continue
            if len(current) >= threshold:
                groups.append(list(current))
            current = [turn]
        else:
            if len(current) >= threshold:
                groups.append(list(current))
            current = []

    if len(current) >= threshold:
        groups.append(list(current))

    items: List[WasteItem] = []
    for group in groups:
        repeats = group[1:]  # the first call is legitimate work
        wasted = sum(t.input_tokens for t in repeats)
        cost = sum(_input_cost(book, t) for t in repeats)
        name = group[0].tools[0] if group[0].tools else "tool"
        target = group[0].targets[0] if group[0].targets else ""
        label = f"{name}({target})" if target else name
        amounts = {t.index: _input_cost(book, t) for t in repeats}
        items.append(
            WasteItem(
                kind="loops",
                headline=f"{label} called {len(group)}x in a row",
                turns=[t.index for t in group],
                tokens_wasted=wasted,
                cost_usd=round(cost, 4),
                fix="Read the file once and patch it, or widen the glob so one call covers every match.",
                _amounts=amounts,
            )
        )
    return items


def _find_cache_misses(turns: List[Turn], book: PriceBook) -> List[WasteItem]:
    """Fresh, large, uncached input in a session that otherwise caches."""
    if not any(t.cache_read_tokens for t in turns):
        return []  # caching was never used, so nothing was "missed"
    items: List[WasteItem] = []
    for turn in turns:
        if turn.cache_read_tokens or turn.input_tokens < CACHE_MISS_MIN_TOKENS:
            continue
        rate: Rate = book.rate_for(turn.model)
        delta = max(0.0, (rate.input - rate.cache_read) / 1_000_000)
        amount = turn.input_tokens * delta
        if amount <= 0:
            continue
        items.append(
            WasteItem(
                kind="cache_misses",
                headline=f"turn {turn.index}: {turn.input_tokens:,} uncached input tokens",
                turns=[turn.index],
                tokens_wasted=int(turn.input_tokens * delta * 1_000_000 / rate.input)
                if rate.input
                else turn.input_tokens,
                cost_usd=round(amount, 4),
                fix="Keep the system prompt and file reads byte-stable, then re-mark the cache breakpoint.",
                _amounts={turn.index: amount},
            )
        )
    items.sort(key=lambda i: i.cost_usd, reverse=True)
    return items[:5]


def _find_context_creep(turns: List[Turn], book: PriceBook) -> List[WasteItem]:
    """Second-half context noticeably fatter than the session baseline, never recovering.

    Compares the median input of the first half with the second half so that one
    genuinely huge file read does not masquerade as creep, then charges only the
    excess above the baseline.
    """
    active = [t for t in turns if t.input_tokens > 0]
    if len(active) < 6:
        return []
    half = len(active) // 2
    first, second = active[:half], active[half:]
    baseline = statistics.median([t.input_tokens for t in first])
    tail = statistics.median([t.input_tokens for t in second])
    if baseline <= 0 or tail < baseline * CREEP_FACTOR or tail - baseline < CREEP_MIN_GROWTH:
        return []
    ceiling = baseline * CREEP_FACTOR
    offenders = [t for t in second if t.input_tokens > ceiling]
    if len(offenders) < 2:
        return []
    excess = int(sum(t.input_tokens - ceiling for t in offenders))
    amounts = {t.index: (t.input_tokens - ceiling) / 1_000_000 * book.rate_for(t.model).input
               for t in offenders}
    cost = sum(amounts.values())
    return [
        WasteItem(
            kind="context_creep",
            headline=(
                f"{len(offenders)} late turns averaged "
                f"{int(statistics.median([int(t.input_tokens) for t in offenders])):,} input tokens "
                f"vs a {int(baseline):,} baseline"
            ),
            turns=[t.index for t in offenders],
            tokens_wasted=excess,
            cost_usd=round(cost, 4),
            fix="Compact the transcript or start a fresh session per task instead of carrying the whole history.",
            _amounts=amounts,
        )
    ]


def analyze_session(
    turns: List[Turn],
    book: Optional[PriceBook] = None,
    loop_threshold: int = LOOP_REPEAT_THRESHOLD,
) -> SpendMap:
    """Compute the full spend map for a list of turns."""
    book = book or PriceBook()
    result = SpendMap(turns=len(turns))
    model_stats: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"turns": 0, "tokens": 0, "cost_usd": 0.0}
    )
    tool_stats: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0.0, "cost_usd": 0.0, "turns": 0}
    )

    for turn in turns:
        turn.cost_usd = book.cost(
            turn.model,
            turn.input_tokens,
            turn.output_tokens,
            turn.cache_read_tokens,
            turn.cache_write_tokens,
        )
        result.input_tokens += turn.input_tokens
        result.output_tokens += turn.output_tokens
        result.cache_read_tokens += turn.cache_read_tokens
        result.cache_write_tokens += turn.cache_write_tokens
        result.total_cost_usd += turn.cost_usd

        bucket = model_stats[turn.model]
        bucket["turns"] += 1
        bucket["tokens"] += turn.total_tokens
        bucket["cost_usd"] += turn.cost_usd

        if turn.tools:
            share = turn.cost_usd / len(turn.tools)
            token_share = turn.total_tokens / len(turn.tools)
            seen: set[str] = set()
            for name in turn.tools:
                entry = tool_stats[name]
                entry["calls"] += 1
                entry["tokens"] += token_share
                entry["cost_usd"] += share
                if name not in seen:
                    entry["turns"] += 1
                    seen.add(name)

    result.total_tokens = result.input_tokens + result.output_tokens
    result.models = {
        name: {
            "turns": int(stats["turns"]),
            "tokens": int(stats["tokens"]),
            "cost_usd": round(stats["cost_usd"], 4),
        }
        for name, stats in sorted(
            model_stats.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    }
    result.tools = sorted(
        (
            ToolCost(
                name=name,
                calls=int(stats["calls"]),
                tokens=int(stats["tokens"]),
                cost_usd=round(stats["cost_usd"], 4),
                turns=int(stats["turns"]),
            )
            for name, stats in tool_stats.items()
        ),
        key=lambda t: t.cost_usd,
        reverse=True,
    )

    result.waste = (
        _find_loops(turns, book, loop_threshold)
        + _find_cache_misses(turns, book)
        + _find_context_creep(turns, book)
    )
    result.waste.sort(key=lambda item: item.cost_usd, reverse=True)
    result.unknown_models = sorted(book.unknown_models)
    return result
