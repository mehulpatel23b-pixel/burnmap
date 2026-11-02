"""Render a SpendMap as human text or stable JSON."""

from __future__ import annotations

import json
from typing import Any, Dict

from .analyze import SpendMap

__all__ = ["render_text", "render_json", "RULES"]

RULES = [
    ("loops", "same tool + same target repeated consecutively", "every repeat after the first"),
    ("cache_misses", "uncached input >= 2,000 tokens in a caching session", "cache read vs input rate"),
    ("context_creep", "second-half input > 1.8x the first-half baseline", "excess above baseline"),
]


def _money(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value > 0:
        return f"${value:.4f}"
    return "$0.00"


def _bar(share: float, width: int = 24) -> str:
    filled = max(0, min(width, round(share * width)))
    return "#" * filled + "." * (width - filled)


def render_text(smap: SpendMap, top: int = 8) -> str:
    """Render a spend map as a plain-text report."""
    lines = []
    lines.append("burnmap - session spend map")
    lines.append("=" * 62)
    lines.append(f"turns           {smap.turns}")
    lines.append(f"tokens          {smap.total_tokens:,}  "
                 f"(in {smap.input_tokens:,} / out {smap.output_tokens:,} / "
                 f"cache read {smap.cache_read_tokens:,} / cache write {smap.cache_write_tokens:,})")
    lines.append(f"total cost      {_money(smap.total_cost_usd)}")
    lines.append(f"cost per turn   {_money(smap.cost_per_turn)}")
    lines.append(f"cache hit rate  {smap.cache_hit_rate * 100:.1f}%"
                 + ("   <-- caching is barely doing anything" if smap.turns and smap.cache_hit_rate < 0.2 else ""))

    if smap.models:
        lines.append("")
        lines.append("COST BY MODEL")
        for name, stats in smap.models.items():
            lines.append(
                f"  {name:<34} {_money(stats['cost_usd']):>10}  "
                f"{stats['turns']:>4} turns  {stats['tokens']:>12,} tok"
            )

    if smap.tools:
        lines.append("")
        lines.append("COST BY TOOL")
        top_cost = max((t.cost_usd for t in smap.tools), default=0.0) or 1.0
        for tool in smap.tools[:top]:
            share = tool.cost_usd / top_cost
            lines.append(
                f"  {tool.name:<24} {_money(tool.cost_usd):>10}  "
                f"{tool.calls:>4} calls  {tool.tokens_per_call:>10,.0f} tok/call  {_bar(share)}"
            )

    lines.append("")
    if smap.waste:
        lines.append("WASTE")
        for item in smap.waste:
            lines.append(
                f"  [{item.kind}] {item.headline}\n"
                f"      ~{_money(item.cost_usd)} over {item.tokens_wasted:,} tokens  "
                f"(turns {', '.join(str(t) for t in item.turns[:8])}"
                f"{'...' if len(item.turns) > 8 else ''})\n"
                f"      fix: {item.fix}"
            )
        lines.append("")
        pct = smap.waste_share * 100
        lines.append(
            f"estimated avoidable spend: {_money(smap.waste_cost_usd)} "
            f"({pct:.1f}% of this session)"
        )
    else:
        lines.append("WASTE")
        lines.append("  none detected by the three built-in rules. Clean session.")

    if smap.unknown_models:
        lines.append("")
        lines.append(
            "note: unknown models priced at the fallback rate - "
            + ", ".join(smap.unknown_models)
            + "  (pass --pricing rates.json to be exact)"
        )

    lines.append("")
    lines.append("how the estimate is built")
    for kind, what, charge in RULES:
        lines.append(f"  {kind:<14} {what}  ->  charge for {charge}")
    return "\n".join(lines)


def render_json(smap: SpendMap) -> str:
    """Render a spend map as JSON with a stable key order."""
    payload: Dict[str, Any] = {
        "turns": smap.turns,
        "total_tokens": smap.total_tokens,
        "total_cost_usd": round(smap.total_cost_usd, 6),
        "cost_per_turn_usd": round(smap.cost_per_turn, 6),
        "cache_hit_rate": round(smap.cache_hit_rate, 4),
        "tokens": {
            "input": smap.input_tokens,
            "output": smap.output_tokens,
            "cache_read": smap.cache_read_tokens,
            "cache_write": smap.cache_write_tokens,
        },
        "models": smap.models,
        "tools": [
            {
                "name": t.name,
                "calls": t.calls,
                "tokens": t.tokens,
                "cost_usd": round(t.cost_usd, 6),
                "tokens_per_call": round(t.tokens_per_call, 2),
            }
            for t in smap.tools
        ],
        "waste": {
            "estimated_waste_usd": round(smap.waste_cost_usd, 6),
            "waste_share": round(smap.waste_share, 4),
            "items": [
                {
                    "kind": i.kind,
                    "headline": i.headline,
                    "turns": i.turns,
                    "tokens_wasted": i.tokens_wasted,
                    "cost_usd": round(i.cost_usd, 6),
                    "fix": i.fix,
                }
                for i in smap.waste
            ],
        },
        "unknown_models": smap.unknown_models,
    }
    return json.dumps(payload, indent=2, sort_keys=False)
