"""USD pricing per 1M tokens, used to convert token counts into money.

Prices are public list prices and they do change. Anything here can be
overridden at runtime with ``--pricing rates.json`` so the map stays accurate
without editing code:

    {
      "claude-sonnet":  {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
      "my-local-model": {"input": 0.0, "output": 0.0,  "cache_read": 0.0, "cache_write": 0.0}
    }

Matching is a case-insensitive substring test on the model name, first hit wins,
longest key first. Unknown models fall back to the ``default`` entry and are
flagged in the report so you never silently trust a guessed rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

__all__ = ["Rate", "PriceBook"]


@dataclass(frozen=True)
class Rate:
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


DEFAULT_RATES: Dict[str, Rate] = {
    # Anthropic
    "claude-opus": Rate(15.00, 75.00, 1.50, 18.75),
    "claude-sonnet": Rate(3.00, 15.00, 0.30, 3.75),
    "claude-3-5-sonnet": Rate(3.00, 15.00, 0.30, 3.75),
    "claude-3-7-sonnet": Rate(3.00, 15.00, 0.30, 3.75),
    "claude-haiku": Rate(0.80, 4.00, 0.08, 1.00),
    "claude-3-5-haiku": Rate(0.80, 4.00, 0.08, 1.00),
    # OpenAI
    "gpt-4o-mini": Rate(0.15, 0.60, 0.075, 0.0),
    "gpt-4o": Rate(2.50, 10.00, 1.25, 0.0),
    "gpt-4.1-mini": Rate(0.40, 1.60, 0.10, 0.0),
    "gpt-4.1": Rate(2.00, 8.00, 0.50, 0.0),
    "o3-mini": Rate(1.10, 4.40, 0.55, 0.0),
    "o3": Rate(2.00, 8.00, 0.50, 0.0),
    # Google
    "gemini-2.5-pro": Rate(1.25, 10.00, 0.31, 0.0),
    "gemini-2.5-flash": Rate(0.30, 2.50, 0.075, 0.0),
    "gemini-2.0-flash": Rate(0.10, 0.40, 0.025, 0.0),
    # Others commonly seen in agent logs
    "deepseek-chat": Rate(0.27, 1.10, 0.07, 0.0),
    "deepseek-reasoner": Rate(0.55, 2.19, 0.14, 0.0),
    "kimi": Rate(0.60, 2.50, 0.15, 0.0),
    "qwen": Rate(0.40, 1.20, 0.10, 0.0),
    "llama": Rate(0.20, 0.60, 0.05, 0.0),
}

# Used only when a model is genuinely unknown. Assumed mid-tier frontier rates.
FALLBACK_RATE = Rate(3.00, 15.00, 0.30, 0.0)


class PriceBook:
    """Resolves a model name to a Rate, tracking which models were guessed."""

    def __init__(self, overrides: Optional[Dict[str, Dict[str, float]]] = None):
        merged: Dict[str, Rate] = dict(DEFAULT_RATES)
        self._override_keys: list[str] = []
        for name, spec in (overrides or {}).items():
            if not isinstance(spec, dict):
                continue
            key = name.lower()
            merged[key] = Rate(
                input=float(spec.get("input", 0.0)),
                output=float(spec.get("output", 0.0)),
                cache_read=float(spec.get("cache_read", 0.0)),
                cache_write=float(spec.get("cache_write", 0.0)),
            )
            if key != "default" and key not in self._override_keys:
                self._override_keys.append(key)
        # Longest key first so "gpt-4o-mini" wins over "gpt-4o".
        self._keys = sorted((k for k in merged if k != "default"), key=len, reverse=True)
        # User overrides are always tested before built-in defaults, otherwise a
        # short override like "claude" would lose to the longer "claude-sonnet".
        self._override_keys.sort(key=len, reverse=True)
        self._rates = merged
        self._default = merged.get("default")
        self.unknown_models: set[str] = set()

    @classmethod
    def from_file(cls, path: str) -> "PriceBook":
        with open(path, "r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def rate_for(self, model: str) -> Rate:
        needle = (model or "").lower()
        for key in self._override_keys:
            if key in needle:
                return self._rates[key]
        for key in self._keys:
            if key in needle:
                return self._rates[key]
        if self._default is not None:
            return self._default
        if model and model != "unknown":
            self.unknown_models.add(model)
        return FALLBACK_RATE

    def cost(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        rate = self.rate_for(model)
        return (
            input_tokens / 1_000_000 * rate.input
            + output_tokens / 1_000_000 * rate.output
            + cache_read_tokens / 1_000_000 * rate.cache_read
            + cache_write_tokens / 1_000_000 * rate.cache_write
        )
