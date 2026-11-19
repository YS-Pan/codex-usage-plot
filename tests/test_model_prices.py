"""The shared price catalog keeps provenance and legacy imports consistent."""

import unittest

import pandas as pd

from codex_usage import cost, model_prices


class ModelPriceCatalogTests(unittest.TestCase):
    def test_cost_uses_the_shared_catalog(self):
        self.assertIs(cost.OFFICIAL_PRICE_HISTORY, model_prices.OFFICIAL_PRICE_HISTORY)
        self.assertIs(cost.OFFICIAL_PRICES, model_prices.OFFICIAL_PRICES)
        self.assertIs(cost.ModelPrice, model_prices.ModelPrice)
        self.assertIs(cost.PricePeriod, model_prices.PricePeriod)

    def test_terra_baseline_records_verified_official_rates(self):
        periods = model_prices.OFFICIAL_PRICE_HISTORY["gpt-6.1-terra"]
        self.assertEqual(len(periods), 1)
        self.assertIsNone(periods[0].effective_from)
        price = periods[0].price
        self.assertEqual((price.input_per_million, price.cached_input_per_million,
                          price.output_per_million), (1.0, 0.05, 5.0))
        self.assertEqual(price.cache_write_multiplier, 1.25)
        self.assertEqual(price.verified_on, "2026-10-29")
        self.assertEqual(price.source, "https://developers.openai.com/api/docs/models/gpt-6.1-terra")
        self.assertIs(model_prices.OFFICIAL_PRICES["gpt-6.1-terra"], price)

    def test_prior_sol_verification_date_is_preserved(self):
        self.assertEqual(model_prices.OFFICIAL_PRICES["gpt-6.1-sol"].verified_on, "2026-10-07")

    def test_gpt_6_2_rates_at_the_long_context_boundary(self):
        events = pd.DataFrame({
            "input_tokens": [272_000, 272_001],
            "cached_input_tokens": [20_000, 20_000],
            "cache_write_input_tokens": [10_000, 10_000],
            "output_tokens": [1_000, 1_000],
        })
        cases = (
            ("gpt-6.2-astra", (8.0, 0.40, 40.0), (2.084, 4.148016)),
            ("gpt-6.2-sol", (2.0, 0.10, 10.0), (0.521, 1.037004)),
        )
        for model, rates, totals in cases:
            with self.subTest(model=model):
                price = model_prices.OFFICIAL_PRICES[model]
                self.assertEqual((price.input_per_million, price.cached_input_per_million,
                                  price.output_per_million), rates)
                self.assertEqual(price.source, f"https://developers.openai.com/api/docs/models/{model}")
                self.assertEqual(price.verified_on, "2026-11-19")

                priced = cost.calculate_model_event_costs(events, model)
                self.assertEqual(priced["long_context"].tolist(), [False, True])
                for actual, expected in zip(priced["total_cost_usd"], totals):
                    self.assertAlmostEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
