"""The shared price catalog keeps provenance and legacy imports consistent."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
