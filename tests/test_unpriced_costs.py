"""Unpriced usage remains visible across hourly and cumulative estimates."""

import unittest

import pandas as pd

from codex_usage.cost import (
    aggregate_recent_hourly_cost, aggregate_thread_model_costs,
    auto_review_scenarios, build_daily_costs, summarize_official_models,
)
from tests.test_recent_hourly_cost import events, hours_for


class UnpricedCostTests(unittest.TestCase):
    def test_missing_breakdown_and_unknown_usage_are_consistent(self):
        frame = events([
            {"timestamp": "2026-10-07T01:00:00Z", "input_tokens": 100_000},
            {"timestamp": "2026-10-07T01:10:00Z", "total_tokens": 900},
            {"timestamp": "2026-10-07T01:20:00Z", "model": "unknown", "total_tokens": 700},
        ])
        hourly = aggregate_recent_hourly_cost(frame, hours_for(frame, "2026-10-08"))
        self.assertAlmostEqual(hourly["official_models_cost_usd"].sum(), 0.2)
        self.assertEqual(hourly["unpriced_requests"].sum(), 2)
        self.assertEqual(hourly["unpriced_tokens"].sum(), 1600)
        task = aggregate_thread_model_costs(frame).set_index("model")
        self.assertEqual(task.loc["gpt-6.1-sol", "unpriced_tokens"], 900)
        detail, priced = summarize_official_models(frame)
        sol = detail.set_index("model").loc["gpt-6.1-sol"]
        self.assertEqual(sol["unpriced_requests"], 1)
        self.assertEqual(sol["unpriced_tokens"], 900)
        self.assertAlmostEqual(priced["total_cost_usd"].sum(), 0.2)
        self.assertEqual(len(priced), 1)

    def test_no_priced_models_and_empty_input_are_valid(self):
        for records in ([], [{"timestamp": "2026-10-07T01:00:00Z", "model": "unknown", "total_tokens": 100}]):
            frame = events(records)
            detail, priced = summarize_official_models(frame)
            self.assertTrue(priced.empty)
            self.assertIn("total_cost_usd", priced)
            self.assertTrue(build_daily_costs(priced, {}).empty)
            if records:
                self.assertEqual(detail.iloc[0]["unpriced_tokens"], 100)

    def test_scenario_only_days_have_zero_official_cost(self):
        frame = events([{"timestamp": "2026-10-07T01:00:00Z", "model": "codex-auto-review", "input_tokens": 100}])
        _, priced = summarize_official_models(frame)
        _, scenarios = auto_review_scenarios(frame, 0)
        daily = build_daily_costs(priced, scenarios)
        self.assertEqual(daily.loc[0, "official_models_cost_usd"], 0)
        self.assertGreater(daily.loc[0, "auto_review_as_gpt-5.6-luna"], 0)
        self.assertFalse(pd.isna(daily.loc[0, "date"]))


if __name__ == "__main__":
    unittest.main()
