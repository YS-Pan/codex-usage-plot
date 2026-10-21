"""Hourly API estimates keep request prices and the token report's local calendar."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb
import numpy as np
import pandas as pd

from codex_usage.charts import (
    COMPOSITION_COLUMNS,
    RAW_TOKEN_COLUMNS,
    aggregate_recent_hourly,
    main as report_main,
    model_stack_colors,
)
from codex_usage.cost import (
    OFFICIAL_PRICES,
    PRICE_DATE,
    aggregate_recent_hourly_cost,
    plot_recent_hourly_cost,
)


ROOT = Path(__file__).resolve().parents[1]
TASK_TEMP = ROOT / ".task-tmp"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def events(records: list[dict]) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {
            "model": "gpt-6.1-sol", "thread_id": "synthetic", "session_id": "synthetic",
            "parent_thread_id": "", "title": "Synthetic task", "source": "desktop", "cwd": "",
            **dict.fromkeys([*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS], 0),
            **record,
        }
        row["total_tokens"] = record.get("total_tokens", row["input_tokens"] + row["output_tokens"])
        row["uncached_input_tokens"] = max(
            0, row["input_tokens"] - row["cached_input_tokens"] - row["cache_write_input_tokens"],
        )
        row["cached_input_component"] = row["cached_input_tokens"]
        row["cache_write_input_component"] = row["cache_write_input_tokens"]
        row["visible_output_tokens"] = row["output_tokens"] - row["reasoning_output_tokens"]
        row["reasoning_output_component"] = row["reasoning_output_tokens"]
        rows.append(row)
    columns = [
        "timestamp", "model", "thread_id", "session_id", "parent_thread_id", "title", "source", "cwd",
        *RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS,
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]:
        frame[column] = frame[column].astype("int64")
    return frame


def hours_for(frame: pd.DataFrame, end: str, zone: ZoneInfo = SHANGHAI) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(aggregate_recent_hourly(frame, zone, pd.Timestamp(end).date())["hour_start"])


class RecentHourlyCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TASK_TEMP.mkdir(exist_ok=True)

    def test_prices_each_request_before_adding_cache_long_context_and_models(self) -> None:
        frame = events([
            {"timestamp": "2026-10-07T01:05:00Z", "input_tokens": 200_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000, "output_tokens": 1_000},
            {"timestamp": "2026-10-07T01:10:00Z", "input_tokens": 200_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000, "output_tokens": 1_000},
            {"timestamp": "2026-10-07T01:20:00Z", "input_tokens": 300_000,
             "cached_input_tokens": 20_000, "cache_write_input_tokens": 10_000, "output_tokens": 1_000},
            {"timestamp": "2026-10-07T02:00:00Z", "model": "gpt-6-luna",
             "input_tokens": 100_000, "output_tokens": 1_000},
            {"timestamp": "2026-10-07T01:30:00Z", "model": "codex-auto-review", "input_tokens": 500},
            {"timestamp": "2026-10-07T01:40:00Z", "model": "unknown", "total_tokens": 700},
            {"timestamp": "2026-10-01T15:59:59Z", "model": "gpt-5.5", "input_tokens": 1_000_000},
            {"timestamp": "2026-10-08T16:00:00Z", "model": "unknown", "total_tokens": 9_000},
        ])
        hours = hours_for(frame, "2026-10-08")
        hourly = aggregate_recent_hourly_cost(frame, hours)
        pd.testing.assert_index_equal(pd.DatetimeIndex(hourly["hour_start"]), hours)
        by_hour = hourly.set_index("hour_start")
        # The two 200K requests remain below the 272K threshold individually.
        # Their costs are $0.377 each; the 300K request uses the long-context rates.
        self.assertAlmostEqual(by_hour.loc["2026-10-07T09:00:00+08:00", "model_gpt-6.1-sol_cost_usd"], 1.903)
        self.assertAlmostEqual(by_hour.loc["2026-10-07T10:00:00+08:00", "model_gpt-6-luna_cost_usd"], 0.0105)
        self.assertAlmostEqual(float(hourly["official_models_cost_usd"].sum()), 1.9135)
        self.assertEqual(int(hourly["unpriced_requests"].sum()), 2)
        self.assertEqual(int(hourly["unpriced_tokens"].sum()), 1_200)
        self.assertNotIn("model_gpt-5.5_cost_usd", hourly)
        self.assertNotIn("model_unknown_cost_usd", hourly)
        self.assertNotIn("model_codex-auto-review_cost_usd", hourly)
        model_columns = [column for column in hourly if column.startswith("model_")]
        np.testing.assert_allclose(hourly[model_columns].sum(axis=1), hourly["official_models_cost_usd"])
        idle = hourly.loc[hourly["hour_start"].dt.day == 4].drop(columns="hour_start")
        self.assertTrue((idle == 0).all().all())

    def test_dst_preserves_both_repeated_hours_and_skips_missing_hour(self) -> None:
        eastern = ZoneInfo("America/New_York")
        cases = [
            ("2026-11-01", ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"], 169, [1, 1]),
            ("2026-03-08", ["2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z"], 167, [1, 3]),
        ]
        for end, timestamps, count, local_hours in cases:
            with self.subTest(end=end):
                frame = events([
                    {"timestamp": timestamp, "input_tokens": amount}
                    for timestamp, amount in zip(timestamps, [100_000, 200_000])
                ])
                hours = hours_for(frame, end, eastern)
                hourly = aggregate_recent_hourly_cost(frame, hours)
                self.assertEqual(len(hourly), count)
                pd.testing.assert_index_equal(pd.DatetimeIndex(hourly["hour_start"]), hours)
                active = hourly.loc[hourly["official_models_cost_usd"] > 0]
                self.assertEqual(active["hour_start"].dt.hour.tolist(), local_hours)
                np.testing.assert_allclose(active["official_models_cost_usd"], [0.2, 0.4])
                if count == 169:
                    self.assertEqual(active["hour_start"].dt.strftime("%z").tolist(), ["-0400", "-0500"])
                else:
                    self.assertNotIn(2, hourly.loc[hourly["hour_start"].dt.day == 8, "hour_start"].dt.hour.tolist())

    def test_plot_uses_full_report_colors_first_seen_order_and_stacked_hourly_costs(self) -> None:
        frame = events([
            {"timestamp": "2026-10-01T00:00:00Z", "model": "gpt-5.5", "input_tokens": 1_000_000},
            {"timestamp": "2026-10-02T00:00:00Z", "model": "unknown", "input_tokens": 100_000},
            {"timestamp": "2026-10-07T01:00:00Z", "model": "gpt-6-luna", "input_tokens": 100_000},
            {"timestamp": "2026-10-07T01:05:00Z", "model": "gpt-6-astra", "input_tokens": 100_000},
            {"timestamp": "2026-10-07T01:10:00Z", "model": "codex-auto-review", "input_tokens": 100_000},
        ])
        hours = hours_for(frame, "2026-10-08")
        hourly = aggregate_recent_hourly_cost(frame, hours)
        with patch("codex_usage.cost.save_figure", return_value=Path("13.png")) as save:
            plot_recent_hourly_cost(hourly, frame, Path("."), "png", 50)
        figure = save.call_args.args[0]
        try:
            axis = figure.axes[0]
            containers = axis.containers
            self.assertEqual([container.get_label().split(":")[0] for container in containers], ["gpt-6-luna", "gpt-6-astra"])
            colors = model_stack_colors(frame)
            bottom = np.zeros(len(hourly))
            for model, container in zip(["gpt-6-luna", "gpt-6-astra"], containers):
                np.testing.assert_allclose([bar.get_y() for bar in container], bottom)
                values = hourly[f"model_{model}_cost_usd"].to_numpy()
                np.testing.assert_allclose([bar.get_height() for bar in container], values)
                np.testing.assert_allclose(container[0].get_facecolor()[:3], to_rgb(colors[model]))
                bottom += values
            self.assertIn("USD", axis.get_ylabel())
            self.assertIn("$1.01", axis.get_title())
            np.testing.assert_allclose(axis.get_xlim(), mdates.date2num([hours[0], hours[-1] + pd.Timedelta(hours=1)]))
        finally:
            plt.close(figure)

    def test_empty_and_unpriced_windows_export_valid_images_and_zero_costs(self) -> None:
        records = [{"timestamp": "2026-10-07T01:00:00Z", "model": "unknown", "input_tokens": 900}]
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            for index, (items, image_format) in enumerate([([], "png"), (records, "svg")]):
                with self.subTest(image_format=image_format, unknown=bool(items)):
                    frame = events(items)
                    hours = hours_for(frame, "2026-10-08")
                    hourly = aggregate_recent_hourly_cost(frame, hours)
                    self.assertEqual(len(hourly), 168)
                    self.assertEqual(float(hourly["official_models_cost_usd"].sum()), 0)
                    self.assertEqual(int(hourly["unpriced_requests"].sum()), len(items))
                    self.assertEqual(int(hourly["unpriced_tokens"].sum()), 900 if items else 0)
                    output = Path(directory) / str(index)
                    output.mkdir()
                    path = plot_recent_hourly_cost(hourly, frame, output, image_format, 40)
                    self.assertEqual(path, output / f"13_recent_hourly_api_cost.{image_format}")
                    if image_format == "png":
                        self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                    else:
                        self.assertIn("<svg", path.read_text(encoding="utf-8"))

    def test_report_exports_same_window_csv_and_summary_for_explicit_end_and_runtime_tail(self) -> None:
        items = [
            {"timestamp": "2026-10-07T00:00:00Z", "type": "session_meta", "payload": {"id": "synthetic"}},
            {"timestamp": "2026-10-07T01:00:00Z", "type": "turn_context",
             "payload": {"turn_id": "token-turn", "model": "gpt-6.1-sol"}},
            {"timestamp": "2026-10-07T01:00:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"input_tokens": 100_000, "output_tokens": 0, "total_tokens": 100_000}},
            }},
            {"timestamp": "2026-10-07T01:10:00Z", "type": "turn_context",
             "payload": {"turn_id": "unknown-turn", "model": "unknown"}},
            {"timestamp": "2026-10-07T01:10:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"input_tokens": 99, "output_tokens": 0, "total_tokens": 99}},
            }},
            {"timestamp": "2026-10-10T01:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "runtime-tail"}},
            {"timestamp": "2026-10-10T02:00:00Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "runtime-tail"}},
        ]
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            home = Path(directory)
            (home / "sessions").mkdir()
            (home / "sessions" / "synthetic.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in items), encoding="utf-8",
            )
            for name, end_arguments, last_date in [
                ("explicit-end", ["--end", "2026-10-12"], "2026-10-12"),
                ("runtime-tail", [], "2026-10-10"),
            ]:
                with self.subTest(name=name):
                    output = home / name
                    with redirect_stdout(io.StringIO()):
                        result = report_main([
                            "--codex-home", str(home), "--output", str(output), "--quiet",
                            "--cache-file", str(home / "cache.sqlite3"), "--timezone", "Asia/Shanghai",
                            "--start", "2026-10-01", "--dpi", "40", "--format", "svg", *end_arguments,
                        ])
                    self.assertEqual(result, 0)
                    path = output / "recent_hourly_api_cost.csv"
                    self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
                    hourly = pd.read_csv(path)
                    token_hourly = pd.read_csv(output / "recent_hourly_usage.csv")
                    self.assertEqual(hourly["hour_start"].tolist(), token_hourly["hour_start"].tolist())
                    self.assertEqual(len(hourly), 168)
                    self.assertEqual(hourly.iloc[-1]["hour_start"], f"{last_date}T23:00:00+08:00")
                    self.assertAlmostEqual(float(hourly["official_models_cost_usd"].sum()), 0.2)
                    self.assertEqual(int(hourly["unpriced_requests"].sum()), 1)
                    self.assertEqual(int(hourly["unpriced_tokens"].sum()), 99)
                    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
                    self.assertIn("13_recent_hourly_api_cost.svg", summary["charts"])
                    estimate = summary["recent_hourly_api_cost"]
                    self.assertEqual(estimate["currency"], "USD")
                    self.assertAlmostEqual(estimate["total_cost_usd"], 0.2)
                    self.assertEqual(estimate["unpriced_requests"], 1)
                    self.assertEqual(estimate["unpriced_tokens"], 99)
                    self.assertEqual(estimate["price_date"], PRICE_DATE)
                    price = estimate["official_prices"]["gpt-6.1-sol"]
                    self.assertEqual(price["source"], OFFICIAL_PRICES["gpt-6.1-sol"].source)
                    self.assertEqual(price["verified_on"], OFFICIAL_PRICES["gpt-6.1-sol"].verified_on)
                    self.assertIn("<svg", (output / "13_recent_hourly_api_cost.svg").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
