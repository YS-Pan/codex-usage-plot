"""Recorded model API prices and their effective history.

Each model starts with a baseline of unknown effective date. Append confirmed
changes in chronological order; see docs/api-cost.md for the pricing policy.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICE_DATE = "2026-10-29"
AUTO_REVIEW_MODEL = "codex-auto-review"
AUTO_REVIEW_SCENARIO_MODELS = (
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra",
)


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    cache_write_multiplier: float = 1.25
    long_context_threshold: int = 272_000
    long_input_multiplier: float = 2.0
    long_output_multiplier: float = 1.5
    source: str = ""
    # Retain the original verification date for existing model prices.
    verified_on: str = "2026-09-23"


@dataclass(frozen=True)
class PricePeriod:
    # None is the compatibility baseline, not a claimed historical start date.
    effective_from: str | None
    price: ModelPrice


# Append confirmed changes here; never replace a model's baseline or old periods.
# Verification dates describe evidence, not when a price became effective.
OFFICIAL_PRICE_HISTORY: dict[str, tuple[PricePeriod, ...]] = {
    "gpt-6-astra": (PricePeriod(None, ModelPrice(
        10.00,
        1.00,
        50.00,
        source="https://developers.openai.com/api/docs/models/gpt-6-astra",
    )),),
    "gpt-6.1-sol": (PricePeriod(None, ModelPrice(
        2.00,
        0.10,
        10.00,
        source="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
        verified_on="2026-10-07",
    )),),
    "gpt-6.1-terra": (PricePeriod(None, ModelPrice(
        1.00,
        0.05,
        5.00,
        source="https://developers.openai.com/api/docs/models/gpt-6.1-terra",
        verified_on=PRICE_DATE,
    )),),
    "gpt-6-sol": (PricePeriod(None, ModelPrice(
        2.00,
        0.20,
        10.00,
        source="https://developers.openai.com/api/docs/models/gpt-6-sol",
    )),),
    "gpt-6-luna": (PricePeriod(None, ModelPrice(
        0.10,
        0.01,
        0.50,
        source="https://developers.openai.com/api/docs/models/gpt-6-luna",
    )),),
    "gpt-5.6-sol": (PricePeriod(None, ModelPrice(
        4.00,
        0.40,
        20.00,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    )),),
    "gpt-5.6-terra": (PricePeriod(None, ModelPrice(
        2.00,
        0.20,
        12.00,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
    )),),
    "gpt-5.6-luna": (PricePeriod(None, ModelPrice(
        0.20,
        0.02,
        1.20,
        source="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    )),),
    "gpt-5.5": (PricePeriod(None, ModelPrice(
        5.00,
        0.50,
        30.00,
        cache_write_multiplier=1.0,
        # The local data contains no GPT-5.5 request above 272K, but the
        # long-context multiplier is retained for future exports. GPT-5.5
        # has no additional cache-write charge, unlike GPT-5.6 and later.
        source="https://developers.openai.com/api/docs/models/gpt-5.5",
    )),),
}

# Preserve the existing public model -> latest recorded price mapping.
OFFICIAL_PRICES: dict[str, ModelPrice] = {
    model: periods[-1].price for model, periods in OFFICIAL_PRICE_HISTORY.items()
}
