"""Turn AI agent session logs into a normalized list of turns.

Supported shapes (auto-detected per line, all JSONL):

1. Anthropic Messages API / Claude Code style
   {"message": {"role": "assistant", "model": "...", "usage": {
        "input_tokens": 1200, "output_tokens": 340,
        "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 400},
        "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}

2. OpenAI Chat Completions / Codex style
   {"model": "...", "usage": {"prompt_tokens": 1200, "completion_tokens": 340,
        "prompt_tokens_details": {"cached_tokens": 8000}},
        "choices": [{"message": {"tool_calls": [{"function": {"name": "read_file",
                    "arguments": "{\"path\": \"a.py\"}"}}]}}]}

3. Flat/minimal shape
   {"role": "assistant", "model": "...", "input_tokens": 1200, "output_tokens": 340,
    "tools": ["Read"]}

Anything without usage numbers is ignored, so mixed logs are safe to feed in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["Turn", "parse_session", "parse_lines"]


@dataclass
class Turn:
    """One assistant response with its token accounting."""

    index: int
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tools: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    timestamp: Optional[str] = None
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_input(self) -> int:
        """Fresh input tokens that were charged at full price."""
        return self.input_tokens

    @property
    def tool_signature(self) -> str:
        """Stable key used for loop detection: tool names + their targets."""
        if not self.tools:
            return ""
        return "|".join(f"{name}:{target}" for name, target in zip(self.tools, self.targets))

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _targets_from_blocks(blocks: Any) -> tuple[List[str], List[str]]:
    """Extract (tool names, targets) from Anthropic-style content blocks."""
    names: List[str] = []
    targets: List[str] = []
    if not isinstance(blocks, list):
        return names, targets
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        names.append(str(block.get("name") or "tool"))
        payload = block.get("input")
        target = ""
        if isinstance(payload, dict):
            for key in ("file_path", "path", "command", "pattern", "url", "query"):
                if payload.get(key):
                    target = str(payload[key])[:120]
                    break
        targets.append(target)
    return names, targets


def _targets_from_tool_calls(calls: Any) -> tuple[List[str], List[str]]:
    """Extract (tool names, targets) from OpenAI-style tool_calls."""
    names: List[str] = []
    targets: List[str] = []
    if not isinstance(calls, list):
        return names, targets
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        names.append(str(fn.get("name") or call.get("name") or "tool"))
        raw = fn.get("arguments") or call.get("arguments")
        target = ""
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = {"raw": raw}
        elif isinstance(raw, dict):
            parsed = raw
        else:
            parsed = {}
        for key in ("file_path", "path", "command", "pattern", "url", "query", "raw"):
            if parsed.get(key):
                target = str(parsed[key])[:120]
                break
        targets.append(target)
    return names, targets


def _normalize_usage(line: Dict[str, Any], message: Dict[str, Any]) -> Optional[Dict[str, int]]:
    usage = message.get("usage") or line.get("usage")
    if not isinstance(usage, dict):
        # Flat shape: accept top-level counters.
        if any(k in line for k in ("input_tokens", "output_tokens", "prompt_tokens")):
            usage = line
        else:
            return None

    details = usage.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0

    input_tokens = _as_int(usage.get("input_tokens") or usage.get("prompt_tokens")) or _as_int(
        line.get("input_tokens") or line.get("prompt_tokens")
    )
    output_tokens = _as_int(usage.get("output_tokens") or usage.get("completion_tokens")) or _as_int(
        line.get("output_tokens") or line.get("completion_tokens")
    )
    cache_read = _as_int(usage.get("cache_read_input_tokens")) or cached
    cache_write = _as_int(usage.get("cache_creation_input_tokens"))

    if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
        return None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _clean_model(value: Any) -> str:
    if not value:
        return "unknown"
    text = str(value).strip()
    # Claude Code sometimes records "claude-sonnet-4-5-20250929[1m]".
    return text.split("[")[0] or "unknown"


def parse_lines(lines) -> List[Turn]:
    """Parse an iterable of raw JSONL lines into turns."""
    turns: List[Turn] = []
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue

        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        role = message.get("role") or line.get("role")
        usage = _normalize_usage(line, message)
        if usage is None:
            continue
        if role and role not in ("assistant", "model"):
            continue

        names, targets = _targets_from_blocks(message.get("content"))
        if not names:
            choices = line.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                inner = first.get("message") if isinstance(first.get("message"), dict) else {}
                names, targets = _targets_from_tool_calls(inner.get("tool_calls"))
        if not names:
            names, targets = _targets_from_tool_calls(line.get("tool_calls"))
        if not names and isinstance(line.get("tools"), list):
            names = [str(t) for t in line["tools"] if isinstance(t, (str, int))]
            targets = [""] * len(names)

        model = _clean_model(message.get("model") or line.get("model"))
        turns.append(
            Turn(
                index=len(turns) + 1,
                model=model,
                timestamp=line.get("timestamp") or line.get("created_at"),
                tools=names,
                targets=targets,
                **usage,
            )
        )
    return turns


def parse_session(path: str) -> List[Turn]:
    """Parse a session JSONL file from disk."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_lines(handle)
