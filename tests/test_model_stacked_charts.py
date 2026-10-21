import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgba

from codex_usage import charts


class ModelStackedChartTests(unittest.TestCase):
    @staticmethod
    def events(records: list[tuple[str, str, str, str, int]], timezone: str = "Asia/Shanghai") -> pd.DataFrame:
        events = pd.DataFrame(
            records, columns=["timestamp", "thread_id", "session_id", "model", "total_tokens"],
        )
        events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True).dt.tz_convert(timezone)
        for column in [*charts.RAW_TOKEN_COLUMNS, *charts.COMPOSITION_COLUMNS]:
            if column not in events:
                events[column] = 0
            events[column] = events[column].astype("int64")
        events["input_tokens"] = events["total_tokens"]
        events["uncached_input_tokens"] = events["total_tokens"]
        events["title"] = events["thread_id"].map(lambda value: f"Task {value}")
        events["source"] = "cli"
        events["cwd"] = "synthetic"
        return events

    def capture(self, plot, *args):
        with patch("codex_usage.charts.save_figure", return_value=Path("synthetic.png")) as saved:
            plot(*args, Path("."), "png", 80)
        figure = saved.call_args.args[0]
        self.addCleanup(plt.close, figure)
        return figure.axes[0]

    def assert_stacks(self, axis, totals, colors, *, horizontal=False):
        total = np.asarray(totals, dtype=float)
        bottom = np.zeros(len(total))
        models = []
        for container in axis.containers:
            model = container.get_label().split(":")[0]
            models.append(model)
            self.assertIn(model, colors)
            self.assertEqual(len(container.patches), len(total))
            values = np.array([
                bar.get_width() if horizontal else bar.get_height()
                for bar in container.patches
            ])
            positions = np.array([
                bar.get_x() if horizontal else bar.get_y()
                for bar in container.patches
            ])
            np.testing.assert_allclose(positions, bottom)
            for bar in container.patches:
                np.testing.assert_allclose(bar.get_facecolor(), to_rgba(colors[model]))
            bottom += values
        self.assertTrue(models)
        self.assertEqual(models, [model for model in colors if model in models])
        np.testing.assert_allclose(bottom, total)

    def test_daily_model_totals_match_cumulative_usage_across_local_midnight_and_idle_days(self):
        events = self.events([
            ("2026-10-05T15:59:59Z", "a", "a", "old", 100),
            ("2026-10-05T16:00:00Z", "a", "a", "old", 200),
            ("2026-10-05T16:01:00Z", "a", "child", "codex-auto-review", 50),
            ("2026-10-07T16:00:00Z", "b", "b", "unknown", 25),
        ])
        dates = pd.date_range("2026-10-04", "2026-10-09", tz="Asia/Shanghai")
        by_model = charts.model_tokens_by_time(events, dates)
        self.assertTrue(by_model.index.equals(dates))
        self.assertEqual(by_model["old"].tolist(), [0, 100, 200, 0, 0, 0])
        self.assertEqual(by_model["codex-auto-review"].tolist(), [0, 0, 50, 0, 0, 0])
        self.assertEqual(by_model["unknown"].tolist(), [0, 0, 0, 0, 25, 0])
        daily = charts.aggregate_daily(events, dates)
        np.testing.assert_array_equal(by_model.sum(axis=1), daily["total_tokens"])
        pd.testing.assert_frame_equal(by_model.cumsum(), charts.cumulative_model_tokens(events, dates))

    def test_hourly_model_totals_preserve_actual_dst_hours(self):
        cases = [
            ("2026-11-01", "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z", 169),
            ("2026-03-08", "2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z", 167),
        ]
        for end, first, second, count in cases:
            with self.subTest(end=end):
                events = self.events([
                    (first, "a", "a", "old", 11),
                    (first, "a", "child", "new", 7),
                    (second, "b", "b", "new", 22),
                    ("2026-01-01T12:00:00Z", "c", "c", "outside", 999),
                ], "America/New_York")
                hourly = charts.aggregate_recent_hourly(events, ZoneInfo("America/New_York"), pd.Timestamp(end).date())
                by_model = charts.model_tokens_by_time(events, pd.DatetimeIndex(hourly["hour_start"]), "h")
                self.assertEqual(len(by_model), count)
                np.testing.assert_array_equal(by_model.sum(axis=1), hourly["total_tokens"])
                self.assertEqual(int(by_model.sum().sum()), 40)
                self.assertEqual(by_model.loc[by_model.sum(axis=1) > 0, "new"].tolist(), [7, 22])
                if end == "2026-11-01":
                    repeated = by_model.loc[(by_model.index.day == 1) & (by_model.index.hour == 1)]
                    self.assertEqual(repeated.index.strftime("%z").tolist(), ["-0400", "-0500"])
                    self.assertEqual(repeated.sum(axis=1).tolist(), [18, 22])
                else:
                    self.assertNotIn(2, by_model.loc[by_model.index.day == 8].index.hour)

    def test_daily_thread_and_hourly_stacks_keep_global_colors_and_total_values(self):
        events = self.events([
            ("2026-10-01T00:00:00Z", "old-a", "old-a", "old", 1000),
            ("2026-10-01T01:00:00Z", "old-b", "old-b", "old", 1000),
            ("2026-10-08T00:05:00Z", "main", "main", "new", 1500),
            ("2026-10-08T00:35:00Z", "main", "nested-child", "codex-auto-review", 100),
        ])
        colors = charts.model_stack_colors(events)
        weekly_colors = charts.breakdown_color_map(charts.weekly_pivot(events, "model").columns)
        self.assertEqual(list(colors), ["old", "new", "codex-auto-review"])
        for model in colors:
            np.testing.assert_allclose(to_rgba(colors[model]), to_rgba(weekly_colors[model]))

        daily = charts.aggregate_daily(events)
        daily_axis = self.capture(charts.plot_daily_usage, daily, events)
        self.assert_stacks(daily_axis, daily["total_tokens"], colors)
        self.assertEqual(len(daily_axis.lines), 1)
        np.testing.assert_allclose(daily_axis.lines[0].get_ydata(), daily["rolling_7d_tokens"])

        threads = charts.aggregate_threads(events)
        thread_axis = self.capture(charts.plot_top_threads, threads, events, 1)
        self.assert_stacks(thread_axis, [1600], colors, horizontal=True)
        self.assertEqual([tick.get_text() for tick in thread_axis.get_yticklabels()], ["synthetic · 2026-10-08\nTask main"])
        contributions = {
            container.get_label().split(":")[0]: container.patches[0].get_width()
            for container in thread_axis.containers
        }
        self.assertEqual(contributions.get("new"), 1500)
        self.assertEqual(contributions.get("codex-auto-review"), 100)
        self.assertEqual(contributions.get("old", 0), 0)
        self.assertTrue(any(text.get_text().strip() == charts.format_tokens(1600) for text in thread_axis.texts))

        hourly = charts.aggregate_recent_hourly(events, ZoneInfo("Asia/Shanghai"), pd.Timestamp("2026-10-08").date())
        hourly_axis = self.capture(charts.plot_recent_hourly_usage, hourly, events)
        self.assert_stacks(hourly_axis, hourly["total_tokens"], colors)
        self.assertEqual(int(hourly["total_tokens"].sum()), 1600)

    def test_empty_data_still_renders_all_three_images(self):
        events = self.events([])
        dates = pd.date_range("2026-10-02", "2026-10-08", tz="Asia/Shanghai")
        daily = charts.aggregate_daily(events, dates)
        hourly = charts.aggregate_recent_hourly(events, ZoneInfo("Asia/Shanghai"), dates[-1].date())
        threads = charts.aggregate_threads(events)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = [
                charts.plot_daily_usage(daily, events, output, "png", 40),
                charts.plot_top_threads(threads, events, 5, output, "png", 40),
                charts.plot_recent_hourly_usage(hourly, events, output, "png", 40),
            ]
            for path in paths:
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
