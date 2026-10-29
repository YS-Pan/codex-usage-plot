"""Daily token-type costs retain per-event pricing and local calendar boundaries."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgba
import numpy as np
import pandas as pd

from codex_usage import charts, cost
from codex_usage.model_prices import ModelPrice, PricePeriod


TASK_TEMP = Path(__file__).resolve().parents[1] / ".task-tmp"


def events(records: list[dict]) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {"model": "gpt-6.1-sol", **dict.fromkeys(charts.RAW_TOKEN_COLUMNS, 0), **record}
        row["total_tokens"] = record.get("total_tokens", row["input_tokens"] + row["output_tokens"])
        rows.append(row)
    frame = pd.DataFrame(rows, columns=["timestamp", "model", *charts.RAW_TOKEN_COLUMNS])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in charts.RAW_TOKEN_COLUMNS:
        frame[column] = frame[column].astype("int64")
    return frame


class DailyCostCompositionTests(unittest.TestCase):
    def test_prices_events_before_daily_sum_and_splits_output_without_double_counting(self) -> None:
        frame = events([
            {"timestamp": "2026-10-07T01:00:00Z", "input_tokens": 200_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000,
             "output_tokens": 1_000, "reasoning_output_tokens": 250},
            {"timestamp": "2026-10-07T02:00:00Z", "input_tokens": 200_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000,
             "output_tokens": 1_000, "reasoning_output_tokens": 250},
            {"timestamp": "2026-10-07T03:00:00Z", "input_tokens": 300_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000,
             "output_tokens": 1_000, "reasoning_output_tokens": 250},
            {"timestamp": "2026-10-07T04:00:00Z", "model": "gpt-6-luna",
             "input_tokens": 100_000, "output_tokens": 1_000, "reasoning_output_tokens": 2_000},
            {"timestamp": "2026-10-08T01:00:00Z", "output_tokens": 500,
             "reasoning_output_tokens": -10},
            {"timestamp": "2026-10-08T02:00:00Z", "input_tokens": 1_000,
             "reasoning_output_tokens": 1_000},
        ])
        dates = pd.date_range("2026-10-06", "2026-10-09", tz="Asia/Shanghai")
        daily = cost.aggregate_daily_cost_composition(frame, dates)
        values = daily.set_index("date")
        first = values.loc["2026-10-07"]
        self.assertAlmostEqual(first["uncached_input_cost_usd"], 1.77)
        self.assertAlmostEqual(first["cached_input_cost_usd"], 0.008)
        self.assertAlmostEqual(first["cache_write_cost_usd"], 0.1)
        self.assertAlmostEqual(first["visible_output_cost_usd"], 0.02625)
        self.assertAlmostEqual(first["reasoning_output_cost_usd"], 0.00925)
        self.assertAlmostEqual(first["official_models_cost_usd"], 1.9135)
        self.assertAlmostEqual(values.loc["2026-10-08", "visible_output_cost_usd"], 0.005)
        self.assertEqual(values.loc["2026-10-08", "reasoning_output_cost_usd"], 0)
        np.testing.assert_allclose(daily[cost.COST_COMPOSITION_COLUMNS].sum(axis=1), daily["official_models_cost_usd"])
        _, priced = cost.summarize_official_models(frame)
        self.assertAlmostEqual(daily["official_models_cost_usd"].sum(), priced["total_cost_usd"].sum())
        self.assertFalse(daily.drop(columns="date").isna().any().any())
        self.assertTrue((values.loc[["2026-10-06", "2026-10-09"]] == 0).all().all())

    def test_local_days_history_boundary_idle_dates_and_excluded_usage(self) -> None:
        frame = events([
            {"timestamp": "2026-10-05T15:59:59Z", "input_tokens": 9_000_000},
            {"timestamp": "2026-10-06T15:59:59Z", "input_tokens": 100_000},
            {"timestamp": "2026-10-06T16:00:00Z", "input_tokens": 100_000},
            {"timestamp": "2026-10-07T01:00:00Z", "model": "unknown", "input_tokens": 600},
            {"timestamp": "2026-10-07T02:00:00Z", "model": "codex-auto-review", "output_tokens": 500},
            {"timestamp": "2026-10-07T03:00:00Z", "total_tokens": 700},
            {"timestamp": "2026-10-07T04:00:00Z", "input_tokens": 100, "total_tokens": 125},
            {"timestamp": "2026-10-09T16:00:00Z", "model": "unknown", "input_tokens": 9_000_000},
        ])
        baseline = ModelPrice(2, 0.1, 10)
        increased = ModelPrice(4, 0.2, 20)
        periods = (PricePeriod(None, baseline), PricePeriod("2026-10-06T16:00:00Z", increased))
        dates = pd.date_range("2026-10-06", "2026-10-09", tz="Asia/Shanghai")
        with patch.dict(cost.OFFICIAL_PRICE_HISTORY, {"gpt-6.1-sol": periods}):
            daily = cost.aggregate_daily_cost_composition(frame, dates)
        pd.testing.assert_index_equal(pd.DatetimeIndex(daily["date"]), dates.rename("date"))
        np.testing.assert_allclose(daily["official_models_cost_usd"], [0.2, 0.4004, 0, 0])
        self.assertEqual(daily["unpriced_requests"].tolist(), [0, 3, 0, 0])
        self.assertEqual(daily["unpriced_tokens"].tolist(), [0, 1_825, 0, 0])
        self.assertEqual(daily["unpriced_requests"].dtype, "int64")

    def test_fall_dst_keeps_both_repeated_hours_in_same_local_day(self) -> None:
        frame = events([
            {"timestamp": "2026-11-01T05:30:00Z", "input_tokens": 100_000},
            {"timestamp": "2026-11-01T06:30:00Z", "input_tokens": 200_000},
            {"timestamp": "2026-11-02T04:59:59Z", "input_tokens": 100_000},
            {"timestamp": "2026-11-02T05:00:00Z", "input_tokens": 100_000},
        ])
        dates = pd.date_range("2026-10-31", "2026-11-02", tz="America/New_York")
        daily = cost.aggregate_daily_cost_composition(frame, dates)
        np.testing.assert_allclose(daily["official_models_cost_usd"], [0, 0.8, 0.2])
        self.assertEqual(daily["date"].dt.strftime("%z").tolist(), ["-0400", "-0400", "-0500"])

    def test_plot_stacks_daily_costs_uses_linear_dollars_and_composition_colors(self) -> None:
        frame = events([
            {"timestamp": "2026-10-06T01:00:00Z", "input_tokens": 100_000,
             "cached_input_tokens": 20_000, "output_tokens": 1_000, "reasoning_output_tokens": 400},
            {"timestamp": "2026-10-08T01:00:00Z", "input_tokens": 200_000,
             "cached_input_tokens": 10_000, "output_tokens": 3_000, "reasoning_output_tokens": 500},
        ])
        dates = pd.date_range("2026-10-06", "2026-10-08", tz="Asia/Shanghai")
        daily = cost.aggregate_daily_cost_composition(frame, dates)
        colors = charts.breakdown_color_map(charts.COMPOSITION_COLUMNS)
        with patch.object(cost, "save_figure", return_value=Path("14.png")) as save:
            cost.plot_daily_cost_composition(daily, Path("."), "png", 40, component_colors=colors)
        figure = save.call_args.args[0]
        try:
            axis = figure.axes[0]
            self.assertEqual(axis.get_yscale(), "linear")
            self.assertEqual(axis.get_ylim()[0], 0)
            self.assertIn("USD", axis.get_ylabel())
            self.assertTrue(axis.yaxis.get_major_formatter()(0.01, 0).startswith("$0.01"))
            self.assertIn("Daily estimated API cost", axis.get_title())
            active = [column for column in cost.COST_COMPOSITION_COLUMNS if daily[column].sum() > 0]
            self.assertEqual(len(axis.containers), len(active))
            bottom = np.zeros(len(daily))
            x_values = mdates.date2num(daily["date"].dt.tz_localize(None))
            for column, container in zip(active, axis.containers):
                values = daily[column].to_numpy()
                self.assertEqual(len(container.patches), len(daily))
                np.testing.assert_allclose([bar.get_y() for bar in container], bottom)
                np.testing.assert_allclose([bar.get_height() for bar in container], values)
                np.testing.assert_allclose([bar.get_x() + bar.get_width() / 2 for bar in container], x_values)
                self.assertTrue(all(0 < bar.get_width() < 1 for bar in container))
                token_column = cost.COST_COMPOSITION_TOKENS[column]
                for bar in container:
                    np.testing.assert_allclose(bar.get_facecolor(), to_rgba(colors[token_column]))
                    self.assertEqual(bar.get_alpha(), 1)
                bottom += values
            np.testing.assert_allclose(bottom, daily["official_models_cost_usd"])
            self.assertEqual(bottom[1], 0)  # Idle day remains zero rather than cumulative.
            self.assertIn("14_daily_api_cost_composition", save.call_args.args)
        finally:
            plt.close(figure)

    def test_empty_unpriced_and_one_day_data_render_valid_formats(self) -> None:
        records = [{"timestamp": "2026-10-07T01:00:00Z", "model": "unknown", "input_tokens": 900}]
        TASK_TEMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            output = Path(directory)
            for frame, dates, image_format in [
                (events([]), pd.date_range("2026-10-06", "2026-10-08", tz="Asia/Shanghai"), "png"),
                (events(records), pd.date_range("2026-10-07", periods=1, tz="Asia/Shanghai"), "svg"),
                (events([]), pd.DatetimeIndex([], tz="Asia/Shanghai"), "pdf"),
                (events([{"timestamp": "2026-10-07T01:00:00Z", "input_tokens": 1_000}]),
                 pd.date_range("2026-10-07", periods=1, tz="Asia/Shanghai"), "png"),
            ]:
                with self.subTest(image_format=image_format, rows=len(frame), dates=len(dates)):
                    daily = cost.aggregate_daily_cost_composition(frame, dates)
                    self.assertEqual(len(daily), len(dates))
                    self.assertFalse(daily.drop(columns="date").isna().any().any())
                    path = cost.plot_daily_cost_composition(daily, output, image_format, 40)
                    self.assertEqual(path.name, f"14_daily_api_cost_composition.{image_format}")
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
