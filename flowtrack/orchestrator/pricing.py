"""Token-to-USD cost estimation per Anthropic model.

VERIFY THESE NUMBERS against https://www.anthropic.com/pricing before relying on
them for budget enforcement — they are placeholders matched to model name.
Treating them as ground truth = budget breach risk.

Pricing is per 1M tokens (Mtok). Cache reads/writes are simplified to "input"
here; refine when needed.
"""

from __future__ import annotations

from decimal import Decimal

# (input_usd_per_mtok, output_usd_per_mtok)
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-4-7": (Decimal("15"), Decimal("75")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}

# Fallback when an unknown model shows up — conservative (use opus rates).
_FALLBACK = (Decimal("15"), Decimal("75"))

_MTOK = Decimal("1000000")


def cost_for(model: str, *, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost in USD for one usage event. Rounds to 4 decimal places."""
    input_rate, output_rate = _PRICING.get(model, _FALLBACK)
    cost = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / _MTOK
    return cost.quantize(Decimal("0.0001"))
