"""API list prices, used to estimate the API-equivalent cost of agent usage.

Subscription plans do not bill per token. The dashboard labels every dollar value
as an API-equivalent estimate for this reason.
"""

import json

from .paths import PRICING_FILE

# USD per million tokens:
# (input, output, cache write 5m, cache write 1h, cache read)
# Cache writes are 1.25x (5m) and 2x (1h) of input. Cache reads differ by model.
# Source: Anthropic model table, cached 2026-10-06.
CLAUDE_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 20.0, 1.00),
    "claude-mythos-5": (10.0, 50.0, 12.5, 20.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 6.0, 0.30),
    # Haiku 5.5 costs more above 100K prompt tokens. This table uses the lower tier.
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
}

_overrides = None


def _load_overrides():
    global _overrides
    if _overrides is None:
        try:
            raw = json.loads(PRICING_FILE.read_text())
            _overrides = {k: tuple(v) for k, v in raw.items()}
        except (OSError, ValueError):
            _overrides = {}
    return _overrides


def price_for(model):
    """Return the price tuple for a model id, or None when the price is unknown."""
    if not model or model.startswith("<"):
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    overrides = _load_overrides()
    if model in overrides:
        return overrides[model]
    if model in CLAUDE_PRICES:
        return CLAUDE_PRICES[model]
    # Longest known prefix wins, so "claude-opus-5-5-xyz" maps to "claude-opus-5-5".
    best = None
    for key in list(overrides) + list(CLAUDE_PRICES):
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    if best:
        return overrides.get(best) or CLAUDE_PRICES[best]
    return None


def cost(model, usage):
    """usage = (input, output, cache_write_5m, cache_write_1h, cache_read)."""
    price = price_for(model)
    if price is None:
        return None
    return sum(u * p for u, p in zip(usage, price)) / 1_000_000
