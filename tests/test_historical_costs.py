"""Synthetic price changes must never reprice usage before their effective instant."""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from codex_usage import animations, charts, cost


MODEL = "gpt-5.6-sol"
CUTOVER = "2030-01-02T00:00:00Z"
TASK_TEMP = Path(__file__).resolve().parents[1] / ".task-tmp"


def events(timestamps, **overrides):
    """Keep timestamps as supplied so the pricing layer handles mixed formats."""
    records = []
    for timestamp in timestamps:
        row = {
            "timestamp": timestamp, "model": MODEL, "thread_id": "synthetic",
            "session_id": "synthetic", "title": "Synthetic task", "source": "cli", "cwd": "",
            **dict.fromkeys([*charts.RAW_TOKEN_COLUMNS, *charts.COMPOSITION_COLUMNS], 0),
            "input_tokens": 100_000,
            **overrides,
        }
        row["total_tokens"] = overrides.get("total_tokens", row["input_tokens"] + row["output_tokens"])
        row["uncached_input_tokens"] = max(
            0, row["input_tokens"] - row["cached_input_tokens"] - row["cache_write_input_tokens"],
        )
        row["cached_input_component"] = row["cached_input_tokens"]
        row["cache_write_input_component"] = row["cache_write_input_tokens"]
        row["visible_output_tokens"] = row["output_tokens"] - row["reasoning_output_tokens"]
        row["reasoning_output_component"] = row["reasoning_output_tokens"]
        records.append(row)
    return pd.DataFrame(records)


class HistoricalCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TASK_TEMP.mkdir(exist_ok=True)

    def setUp(self):
        # These deliberately invented rates exist only within test patches.
        self.old = cost.ModelPrice(2.0, 0.2, 10.0, source="https://example.invalid/old")
        self.new = cost.ModelPrice(4.0, 0.4, 20.0, source="https://example.invalid/new")
        self.history = (
            cost.PricePeriod(None, self.old),
            cost.PricePeriod(CUTOVER, self.new),
        )

    def install_history(self, periods=None):
        patcher = patch.dict(cost.OFFICIAL_PRICE_HISTORY, {MODEL: periods or self.history})
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_current_tables_preserve_fixed_price_results_for_all_models(self):
        frame = events(
            ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"],
            input_tokens=300_000, cached_input_tokens=100_000,
            cache_write_input_tokens=20_000, output_tokens=3_000,
        )
        for model, price in cost.OFFICIAL_PRICES.items():
            with self.subTest(model=model):
                actual = cost.calculate_model_event_costs(frame, model)
                expected = cost.calculate_event_costs(frame, price)
                pd.testing.assert_frame_equal(actual, expected)
                periods = cost.OFFICIAL_PRICE_HISTORY[model]
                self.assertEqual(len(periods), 1)
                self.assertIsNone(periods[0].effective_from)
                self.assertEqual(periods[0].price, price)

    def test_utc_boundary_offsets_and_naive_dates_preserve_order_and_duplicate_index(self):
        self.install_history()
        frame = events([
            CUTOVER,
            "2030-01-01T23:59:59.999999Z",
            "2030-01-02T08:00:00+08:00",
            "2030-01-01T19:00:00-05:00",
            "2030-01-02T07:59:59+08:00",
            "2030-01-02",  # Date-only and naive times mean UTC.
            "2030-01-02T00:00:00",
        ])
        frame.index = pd.Index([8, 2, 8, 3, 2, 9, 0], name="original_row")
        original = frame.copy(deep=True)
        actual = cost.calculate_model_event_costs(frame, MODEL)
        np.testing.assert_allclose(actual["total_cost_usd"], [0.4, 0.2, 0.4, 0.4, 0.2, 0.4, 0.4])
        pd.testing.assert_frame_equal(frame, original)
        pd.testing.assert_frame_equal(actual[original.columns], original)

    def test_missing_and_invalid_timestamps_use_baseline_and_fixed_price_api_still_works(self):
        self.install_history()
        frame = events([None, pd.NaT, np.nan, "", "not-a-timestamp"])
        actual = cost.calculate_model_event_costs(frame, MODEL)
        np.testing.assert_allclose(actual["total_cost_usd"], [0.2] * len(frame))
        without_time = frame.drop(columns="timestamp")
        pd.testing.assert_frame_equal(
            cost.calculate_model_event_costs(without_time, MODEL),
            cost.calculate_event_costs(without_time, self.old),
        )
        # Explicit prices retain their old meaning, even after a dated change.
        dated = events([CUTOVER])
        self.assertAlmostEqual(cost.calculate_event_costs(dated, self.old)["total_cost_usd"].iloc[0], 0.2)

    def test_multiple_periods_apply_latest_effective_rate_without_reordering(self):
        third = replace(self.old, input_per_million=1.0)
        self.install_history((*self.history, cost.PricePeriod("2030-01-03T00:00:00Z", third)))
        frame = events(["2030-01-04", "2030-01-01", "2030-01-02T12:00:00Z", "2030-01-03"])
        np.testing.assert_allclose(
            cost.calculate_model_event_costs(frame, MODEL)["total_cost_usd"], [0.1, 0.2, 0.4, 0.1],
        )

    def test_ambiguous_or_invalid_histories_are_rejected_instead_of_silently_pricing(self):
        baseline, future = self.history
        cases = {
            "empty": (),
            "no baseline": (future,),
            "second baseline": (baseline, cost.PricePeriod(None, self.new)),
            "invalid start": (baseline, cost.PricePeriod("not-a-date", self.new)),
            "missing start": (baseline, cost.PricePeriod("NaT", self.new)),
            "descending": (baseline, future, cost.PricePeriod("2030-01-01", self.old)),
            "duplicate instant": (baseline, future, cost.PricePeriod("2030-01-02T08:00:00+08:00", self.old)),
        }
        frame = events([CUTOVER])
        for name, periods in cases.items():
            with self.subTest(name=name), patch.dict(cost.OFFICIAL_PRICE_HISTORY, {MODEL: periods}):
                with self.assertRaises(ValueError):
                    cost.calculate_model_event_costs(frame, MODEL)

    def test_empty_event_frame_keeps_fixed_price_output_schema(self):
        self.install_history()
        frame = events([CUTOVER]).iloc[:0]
        pd.testing.assert_frame_equal(
            cost.calculate_model_event_costs(frame, MODEL),
            cost.calculate_event_costs(frame, self.old),
        )

    def test_period_selects_cache_write_and_long_context_rules_as_well_as_prices(self):
        changed = cost.ModelPrice(
            3.0, 0.3, 15.0, cache_write_multiplier=1.5,
            long_context_threshold=100_000, long_input_multiplier=3.0,
            long_output_multiplier=2.0,
        )
        self.install_history((self.history[0], cost.PricePeriod(CUTOVER, changed)))
        frame = events(
            ["2030-01-01", CUTOVER, "2030-01-02T00:00:01Z"],
            input_tokens=100_001, cached_input_tokens=20_000,
            cache_write_input_tokens=10_000, output_tokens=1_000,
        )
        frame.loc[1, "input_tokens"] = 100_000  # The threshold itself is not long context.
        actual = cost.calculate_model_event_costs(frame, MODEL)
        self.assertEqual(actual["long_context"].tolist(), [False, False, True])
        np.testing.assert_allclose(actual["total_cost_usd"], [0.179002, 0.276, 0.813009])
        np.testing.assert_allclose(actual["cache_write_cost_usd"], [0.025, 0.045, 0.135])

    def test_task_model_daily_and_hourly_totals_agree_across_cutover(self):
        self.install_history()
        frame = events(["2030-01-01T23:59:59Z", CUTOVER])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert("Asia/Shanghai")
        detail, priced = cost.summarize_official_models(frame)
        task = cost.aggregate_thread_model_costs(frame).iloc[0]
        daily = cost.build_daily_costs(priced, {})
        hours = pd.date_range("2030-01-02T07:00:00", periods=3, freq="h", tz="Asia/Shanghai")
        hourly = cost.aggregate_recent_hourly_cost(frame, hours)
        self.assertAlmostEqual(task["total_cost_usd"], 0.6)
        self.assertEqual(task["priced_tokens"], 200_000)
        self.assertEqual(task["unpriced_tokens"], 0)
        self.assertAlmostEqual(detail.iloc[0]["total_cost_usd"], 0.6)
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(daily.iloc[0]["official_models_cost_usd"], 0.6)
        np.testing.assert_allclose(hourly["official_models_cost_usd"], [0.2, 0.4, 0.0])
        pd.testing.assert_index_equal(pd.DatetimeIndex(hourly["hour_start"]), hours.rename("hour_start"))

    def test_summary_rates_describe_used_periods_and_unknown_models_remain_unpriced(self):
        self.install_history()
        before = events(["2030-01-01"])
        after = events([CUTOVER])
        for frame, expected_rate in [(before, 2.0), (after, 4.0)]:
            with self.subTest(rate=expected_rate):
                row = cost.summarize_official_models(frame)[0].iloc[0]
                self.assertEqual(row["price_period_count"], 1)
                self.assertEqual(row["input_rate_per_million"], expected_rate)
        unknown = events([CUTOVER], model="unknown")
        detail, priced = cost.summarize_official_models(pd.concat([before, after, unknown], ignore_index=True))
        by_model = detail.set_index("model")
        self.assertEqual(by_model.loc[MODEL, "price_period_count"], 2)
        for field in ("input_rate_per_million", "cached_input_rate_per_million", "output_rate_per_million"):
            self.assertTrue(pd.isna(by_model.loc[MODEL, field]))
        self.assertEqual(by_model.loc["unknown", "pricing_status"], "no_public_api_price")
        self.assertTrue(pd.isna(by_model.loc["unknown", "total_cost_usd"]))
        self.assertEqual(len(priced), 2)

    def test_auto_review_scenario_uses_assumed_models_history_without_becoming_official(self):
        self.install_history()
        frame = events(["2030-01-01", CUTOVER], model=cost.AUTO_REVIEW_MODEL)
        scenarios, calculated = cost.auto_review_scenarios(frame, 1.0)
        scenario = f"auto_review_as_{MODEL}"
        row = scenarios.set_index("scenario").loc[scenario]
        self.assertAlmostEqual(row["auto_review_cost_usd"], 0.6)
        self.assertAlmostEqual(row["all_usage_total_usd"], 1.6)
        np.testing.assert_allclose(calculated[scenario]["total_cost_usd"], [0.2, 0.4])
        self.assertEqual(calculated[scenario]["model"].tolist(), [cost.AUTO_REVIEW_MODEL] * 2)
        self.assertNotIn(cost.AUTO_REVIEW_MODEL, cost.OFFICIAL_PRICES)

    def test_cost_animation_prices_historical_events_before_daily_aggregation(self):
        self.install_history()
        frame = events(["2030-01-01T12:00:00Z", CUTOVER])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        zone = ZoneInfo("UTC")
        observed = datetime(2030, 1, 3, tzinfo=timezone.utc)
        options = animations.AnimationOptions(start=date(2030, 1, 1), end=date(2030, 1, 2), dpi=30)
        dates = animations.animation_dates(options, zone, observed)
        renderer = animations.FrameRenderer(frame, [], zone, date(2030, 1, 1), dates, options, 3, observed)
        with patch.object(cost, "plot_cumulative_cost", wraps=cost.plot_cumulative_cost) as plot:
            for stamp in dates:
                with renderer.render(9, stamp):
                    pass
        totals = [float(call.args[0]["official_models_cost_usd"].sum()) for call in plot.call_args_list]
        np.testing.assert_allclose(totals, [0.2, 0.6])

    def test_cost_cli_exports_history_and_mixed_rate_summary(self):
        self.install_history()
        frame = events(["2030-01-01T23:59:59+00:00", "2030-01-02T00:00:00+00:00"])
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            root = Path(directory)
            source = root / "events.csv"
            frame.to_csv(source, index=False)
            with redirect_stdout(io.StringIO()), patch.object(cost, "plot_cumulative_cost"):
                result = cost.main(["--events", str(source), "--output", str(root / "report")])
            self.assertEqual(result, 0)
            report = json.loads((root / "report" / "api_cost_estimate.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(report["priced_subtotal_usd"], 0.6)
            self.assertIn("official_prices", report)
            periods = report["official_price_history"][MODEL]
            self.assertEqual([period["effective_from"] for period in periods], [None, CUTOVER])
            self.assertEqual([period["price"]["input_per_million"] for period in periods], [2.0, 4.0])
            self.assertEqual([period["price"]["source"] for period in periods], [self.old.source, self.new.source])
            csv_path = root / "report" / "api_cost_by_model.csv"
            self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            summary = pd.read_csv(csv_path).iloc[0]
            self.assertEqual(summary["price_period_count"], 2)
            self.assertTrue(pd.isna(summary["input_rate_per_million"]))
            daily = pd.read_csv(root / "report" / "daily_api_cost.csv")
            np.testing.assert_allclose(daily["official_models_cost_usd"], [0.2, 0.4])


if __name__ == "__main__":
    unittest.main()
