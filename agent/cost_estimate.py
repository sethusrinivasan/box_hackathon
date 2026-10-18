"""
cost_estimate.py — transparent, frugal cost estimation for the live review path.

The public web demo runs OFFLINE with a canned model (zero marginal cost). If a
user asks for a "live" run, we do NOT silently call Bedrock and bill the account.
Instead we:

  1. Estimate what the live run WOULD cost (Bedrock input/output tokens on the
     configured model), and
  2. Return that estimate alongside a MOCK result, with a clear note that a real
     run requires cloning the repo and supplying their own credentials.

This keeps the hosted demo free and makes the economics explicit to the user.

Pricing is approximate, declared here as a small table (USD per 1M tokens), and
easy to update. Token counts are estimated from character length (~4 chars/token)
rather than a tokenizer, which is accurate enough for a cost heads-up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# USD per 1,000,000 tokens (input, output). Approximate on-demand Bedrock pricing;
# update as needed. Keys are matched by substring against BEDROCK_MODEL_ID so
# region-prefixed IDs (e.g. "us.amazon.nova-lite-v1:0") resolve correctly.
_PRICING: Dict[str, tuple[float, float]] = {
    # Amazon Nova family — cheapest that serves this task.
    "nova-micro": (0.035, 0.14),
    "nova-lite": (0.06, 0.24),
    "nova-pro": (0.80, 3.20),
    # Anthropic Claude — stronger but pricier.
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet": (3.00, 15.00),
}

# Fallback if the model isn't in the table: assume a cheap-model price so we
# never understate wildly, but flag it as an estimate.
_DEFAULT_PRICE = (0.06, 0.24)

_CHARS_PER_TOKEN = 4.0


@dataclass
class CostEstimate:
    model_id: str
    input_tokens: int
    output_tokens: int
    input_usd: float
    output_usd: float
    total_usd: float
    priced_from: str  # which pricing key matched, or "default"

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_usd": round(self.input_usd, 6),
            "output_usd": round(self.output_usd, 6),
            "total_usd": round(self.total_usd, 6),
            "total_usd_display": f"${self.total_usd:.4f}",
            "priced_from": self.priced_from,
            "note": (
                "Estimated cost of a REAL Bedrock run on this model. The hosted "
                "demo returns a mock result and does not make this call. To run "
                "it for real, clone the repo and use your own AWS credentials."
            ),
        }


def _price_for(model_id: str) -> tuple[tuple[float, float], str]:
    mid = (model_id or "").lower()
    for key, price in _PRICING.items():
        if key in mid:
            return price, key
    return _DEFAULT_PRICE, "default"


def estimate_tokens(text: str) -> int:
    """Rough token count from character length (~4 chars/token)."""
    return max(1, int(len(text or "") / _CHARS_PER_TOKEN))


def estimate_cost(
    model_id: str,
    *,
    input_text: str = "",
    input_tokens: int | None = None,
    output_tokens: int = 1000,
) -> CostEstimate:
    """Estimate the USD cost of one live Bedrock review.

    Provide either input_text (tokens are estimated from it) or input_tokens.
    output_tokens defaults to a typical structured-review response size.
    """
    (in_rate, out_rate), priced_from = _price_for(model_id)
    in_tok = input_tokens if input_tokens is not None else estimate_tokens(input_text)
    in_usd = in_tok / 1_000_000 * in_rate
    out_usd = output_tokens / 1_000_000 * out_rate
    return CostEstimate(
        model_id=model_id,
        input_tokens=in_tok,
        output_tokens=output_tokens,
        input_usd=in_usd,
        output_usd=out_usd,
        total_usd=in_usd + out_usd,
        priced_from=priced_from,
    )
