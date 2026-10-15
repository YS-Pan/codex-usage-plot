import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from codex_usage.running_time import (
    RunningInterval,
    RunningIntervalTracker,
    RuntimeParseStats,
    aggregate_daily_running_time,
    deduplicate_running_intervals,
    merge_running_intervals,
)
from codex_usage.charts import (
    collect_usage_data,
    main,
    plot_daily_running_time,
    running_time_line_coordinates,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def interval(start: str, end: str, **kwargs) -> RunningInterval:
    return RunningInterval(instant(start), instant(end), **kwargs)


class RunningTimeChartTests(unittest.TestCase):
    @staticmethod
    def daily(merged: list[float], summed: list[float]) -> pd.DataFrame:
        frame = pd.DataFrame({
            "date": pd.date_range("2026-08-29", periods=len(merged), tz=SHANGHAI),
            "running_hours": merged,
            "summed_running_hours": summed,
            "day_seconds": 86400,
            "is_complete_day": True,
        })
        frame["rolling_7d_running_hours"] = frame["running_hours"].rolling(7, min_periods=1).mean()
        return frame

    @staticmethod
    def figure(daily: pd.DataFrame) -> plt.Figure:
        with patch("codex_usage.charts.save_figure", return_value=Path("chart.png")) as save:
            plot_daily_running_time(daily, Path("."), SHANGHAI, "png", 80)
        return save.call_args.args[0]

    def test_shared_piecewise_axes_keep_detail_outliers_and_invertible_hours(self) -> None:
        for peak in (25, 48, 288, 10000):
            with self.subTest(peak=peak):
                daily = self.daily([6, 12, 24], [6, 24, peak])
                daily.loc[2, "is_complete_day"] = False
                if peak == 25:
                    daily.loc[2, "running_hours"] = 25
                    daily.loc[2, "day_seconds"] = 90000
                figure = self.figure(daily)
                try:
                    figure.canvas.draw()
                    left, right = figure.axes
                    limit = left.get_ylim()[1]
                    self.assertGreater(limit, peak)
                    self.assertEqual(left.get_ylim(), right.get_ylim())
                    np.testing.assert_array_equal(left.get_yticks(), right.get_yticks())
                    probe = np.array([0, 4, 12, 24, 24 + (limit - 24) / 2, limit])
                    left_pixels = left.transData.transform(np.column_stack((np.zeros(len(probe)), probe)))
                    right_pixels = right.transData.transform(np.column_stack((np.zeros(len(probe)), probe)))
                    np.testing.assert_allclose(left_pixels[:, 1], right_pixels[:, 1])
                    height = left_pixels[-1, 1] - left_pixels[0, 1]
                    self.assertAlmostEqual((left_pixels[3, 1] - left_pixels[0, 1]) / height, 0.52)
                    self.assertAlmostEqual(left_pixels[2, 1] - left_pixels[0, 1], 3 * (left_pixels[1, 1] - left_pixels[0, 1]))
                    self.assertAlmostEqual(left_pixels[4, 1] - left_pixels[3, 1], left_pixels[5, 1] - left_pixels[4, 1])
                    self.assertTrue(np.all(np.diff(left_pixels[:, 1]) > 0))
                    np.testing.assert_allclose(left.transData.inverted().transform(left_pixels)[:, 1], probe, atol=1e-9)
                    knee = left.yaxis.get_transform().transform(np.array([24 - 1e-9, 24, 24 + 1e-9]))
                    np.testing.assert_allclose(knee, [24, 24, 24], atol=1e-8, rtol=0)
                    self.assertEqual(left.containers[0].patches[-1].get_hatch(), "///")
                finally:
                    plt.close(figure)

    def test_crossing_lines_interpolate_at_the_actual_24_hour_crossing(self) -> None:
        dates = pd.Series(pd.date_range("2026-08-29", periods=3))
        x, y = running_time_line_coordinates(dates, pd.Series([12, 36, 6]))
        days = mdates.date2num(dates.to_numpy())
        np.testing.assert_allclose(x, [days[0], days[0] + 0.5, days[1], days[1] + 0.4, days[2]])
        np.testing.assert_array_equal(y, [12, 24, 36, 24, 6])
        for hours in ([0, 24, 48], [12, np.nan, 36]):
            with self.subTest(hours=hours):
                x, y = running_time_line_coordinates(dates, pd.Series(hours))
                np.testing.assert_array_equal(x, days)
                np.testing.assert_allclose(y, hours, equal_nan=True)

    def test_idle_empty_and_ordinary_days_use_readable_linear_axes(self) -> None:
        frames = (self.daily([0, 0], [0, 0]), self.daily([], []), self.daily([2, 12], [4, 24]))
        for daily in frames:
            with self.subTest(rows=len(daily), active=daily["summed_running_hours"].sum()):
                figure = self.figure(daily)
                try:
                    figure.canvas.draw()
                    left, right = figure.axes
                    self.assertEqual(left.get_ylim(), (0, 24))
                    self.assertEqual(right.get_ylim(), (0, 24))
                    np.testing.assert_array_equal(left.yaxis.get_transform().transform([0, 12, 24]), [0, 12, 24])
                    if not daily["summed_running_hours"].gt(0).any():
                        self.assertIn("No running activity in this date range", [text.get_text() for text in left.texts])
                finally:
                    plt.close(figure)


class DailyRunningTimeTests(unittest.TestCase):
    def test_overlapping_duplicate_and_touching_sessions_count_once(self) -> None:
        runs = [
            interval("2026-08-29T01:00:00+08:00", "2026-08-29T03:00:00+08:00", session_id="a"),
            interval("2026-08-29T02:00:00+08:00", "2026-08-29T04:00:00+08:00", session_id="b"),
            interval("2026-08-29T02:15:00+08:00", "2026-08-29T02:30:00+08:00", session_id="c"),
            interval("2026-08-29T04:00:00+08:00", "2026-08-29T05:00:00+08:00", session_id="d"),
        ]
        runs.append(runs[0])
        daily = aggregate_daily_running_time(runs, SHANGHAI, date(2026, 8, 29), date(2026, 8, 29))
        self.assertEqual(len(merge_running_intervals(runs)), 1)
        self.assertEqual(daily.loc[0, "running_seconds"], 4 * 3600)
        self.assertAlmostEqual(daily.loc[0, "running_percent"], 100 / 6)

    def test_splits_at_local_midnight_and_keeps_idle_days(self) -> None:
        runs = [
            interval("2026-08-29T23:00:00+08:00", "2026-08-30T02:00:00+08:00"),
            interval("2026-08-30T01:00:00+08:00", "2026-08-30T04:00:00+08:00"),
        ]
        daily = aggregate_daily_running_time(runs, SHANGHAI, date(2026, 8, 28), date(2026, 8, 31))
        self.assertEqual(daily["running_hours"].tolist(), [0, 1, 4, 0])
        self.assertEqual(daily["summed_running_hours"].tolist(), [0, 1, 5, 0])
        self.assertTrue((daily["day_seconds"] == 86400).all())
        clipped = aggregate_daily_running_time(runs, SHANGHAI, date(2026, 8, 30), date(2026, 8, 30))
        self.assertEqual(clipped.loc[0, "running_hours"], 4)
        self.assertEqual(clipped.loc[0, "summed_running_hours"], 5)

    def test_summed_time_keeps_concurrent_sessions_and_can_exceed_24_hours(self) -> None:
        runs = (
            interval(
                "2026-08-29T00:00:00+08:00", "2026-08-30T00:00:00+08:00",
                session_id=session_id, turn_id=session_id,
            )
            for session_id in ("session-a", "session-b")
        )
        daily = aggregate_daily_running_time(
            runs, SHANGHAI, date(2026, 8, 29), date(2026, 8, 29),
            instant("2026-08-30T00:00:00+08:00"),
        )
        self.assertEqual(daily.loc[0, "running_hours"], 24)
        self.assertEqual(daily.loc[0, "summed_running_hours"], 48)

    def test_multi_day_interval_excludes_its_midnight_endpoint(self) -> None:
        runs = [interval("2026-08-28T12:00:00+08:00", "2026-08-31T00:00:00+08:00")]
        daily = aggregate_daily_running_time(runs, SHANGHAI, date(2026, 8, 29), date(2026, 8, 31))
        self.assertEqual(daily["running_percent"].tolist(), [100, 100, 0])

    def test_today_uses_full_day_denominator_and_stops_at_observation(self) -> None:
        observed_at = instant("2026-08-29T12:00:00+08:00")
        runs = [interval("2026-08-29T00:00:00+08:00", "2026-08-30T00:00:00+08:00")]
        daily = aggregate_daily_running_time(
            runs, SHANGHAI, date(2026, 8, 29), date(2026, 8, 29), observed_at
        )
        self.assertEqual(daily.loc[0, "running_percent"], 50)
        self.assertEqual(daily.loc[0, "summed_running_hours"], 12)
        self.assertEqual(daily.loc[0, "day_seconds"], 86400)
        self.assertFalse(daily.loc[0, "is_complete_day"])

    def test_dst_days_use_elapsed_seconds_without_exceeding_100_percent(self) -> None:
        eastern = ZoneInfo("America/New_York")
        for day, hours in [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)]:
            with self.subTest(day=day):
                start = datetime.combine(day, datetime.min.time(), eastern)
                end = datetime.combine(day + timedelta(days=1), datetime.min.time(), eastern)
                daily = aggregate_daily_running_time(
                    [RunningInterval(start, end)], eastern, day, day,
                    instant("2026-12-01T00:00:00Z"),
                )
                self.assertEqual(daily.loc[0, "running_hours"], hours)
                self.assertEqual(daily.loc[0, "running_percent"], 100)

        # During the repeated hour, wall-clock end can precede wall-clock start.
        start = datetime(2026, 11, 1, 1, 45, tzinfo=eastern, fold=0)
        end = datetime(2026, 11, 1, 1, 15, tzinfo=eastern, fold=1)
        merged = merge_running_intervals([RunningInterval(start, end)])
        self.assertEqual((merged[0][1] - merged[0][0]).total_seconds(), 1800)


class RunningIntervalParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = RuntimeParseStats()
        self.tracker = RunningIntervalTracker("test.jsonl", self.stats)

    def observe(self, kind: str, timestamp: str, **payload) -> None:
        self.tracker.observe("event_msg", {"type": kind, **payload}, timestamp)

    def test_prefers_recorded_duration_over_delayed_log_writes(self) -> None:
        start = instant("2026-08-29T01:00:00Z")
        self.observe("task_started", "2026-08-29T03:00:00Z", turn_id="turn-1", started_at=start.timestamp())
        self.observe(
            "task_complete", "2026-08-29T03:00:00Z", turn_id="turn-1",
            started_at=start.timestamp(), completed_at=start.timestamp() + 60,
            duration_ms=60_500, last_agent_message="PRIVATE CONTENT",
        )
        result = self.tracker.intervals[0]
        self.assertEqual(result.start, start)
        self.assertEqual((result.end - result.start).total_seconds(), 60.5)
        self.assertEqual(result.timing_source, "recorded_duration")
        self.assertNotIn("PRIVATE CONTENT", repr(result))

    def test_abort_uses_boundary_timestamps_and_ends_the_turn(self) -> None:
        self.observe("task_started", "2026-08-29T01:00:00Z", turn_id="turn-1")
        self.observe("turn_aborted", "2026-08-29T01:00:30Z", turn_id="turn-1")
        self.observe("token_count", "2026-08-29T02:00:00Z")
        self.tracker.finish_open_turn()
        result = self.tracker.intervals[0]
        self.assertEqual(result.status, "aborted")
        self.assertEqual((result.end - result.start).total_seconds(), 30)
        self.assertEqual(len(self.tracker.intervals), 1)

    def test_completion_can_recover_a_missing_start_marker(self) -> None:
        end = instant("2026-08-29T02:00:00Z")
        self.observe(
            "task_complete", end.isoformat(), turn_id="turn-1",
            completed_at=end.timestamp(), duration_ms=3_600_000,
        )
        result = self.tracker.intervals[0]
        self.assertEqual(result.start, end - timedelta(hours=1))
        self.assertEqual(result.end, end)
        self.assertEqual(self.stats.starts_recovered_from_duration, 1)

    def test_unfinished_turn_stops_at_last_activity_without_counting_idle_gap(self) -> None:
        self.observe("task_started", "2026-08-29T01:00:00Z", turn_id="old")
        self.tracker.observe("response_item", {"content": "PRIVATE CONTENT"}, "2026-08-29T01:10:00Z")
        self.observe("thread_settings_applied", "2026-08-30T01:00:00Z")
        self.observe("task_started", "2026-08-31T01:00:00Z", turn_id="new")
        self.observe("task_complete", "2026-08-31T01:05:00Z", turn_id="new")
        self.tracker.finish_open_turn()
        old, new = self.tracker.intervals
        self.assertEqual(old.status, "incomplete")
        self.assertEqual((old.end - old.start).total_seconds(), 600)
        self.assertEqual((new.end - new.start).total_seconds(), 300)

    def test_completed_copy_takes_precedence_over_unfinished_fork(self) -> None:
        completed = interval("2026-08-29T01:00:00Z", "2026-08-29T02:00:00Z", turn_id="same")
        unfinished = interval(
            "2026-08-29T01:00:00Z", "2026-08-30T02:00:00Z", turn_id="same",
            status="incomplete", timing_source="last_observed_event",
        )
        result = deduplicate_running_intervals([unfinished, completed, completed], self.stats)
        self.assertEqual(result, [completed])
        self.assertEqual(self.stats.duplicate_intervals_removed, 2)
        self.assertEqual(self.stats.incomplete_intervals, 0)

    def test_invalid_timing_is_skipped_without_inventing_a_duration(self) -> None:
        self.observe("task_started", "invalid", turn_id="bad")
        self.observe("task_complete", "invalid", turn_id="bad", duration_ms=-5)
        self.assertEqual(self.tracker.intervals, [])
        self.assertEqual(self.stats.invalid_intervals, 2)

    def test_runtime_only_logs_export_and_archived_copies_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            (root / "archived_sessions").mkdir()
            items = [
                {"timestamp": "2026-08-29T00:00:00Z", "type": "session_meta", "payload": {"id": "s1"}},
                {"timestamp": "2026-08-29T01:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
                {"timestamp": "2026-08-29T02:00:00Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "PRIVATE CONTENT"}},
            ]
            content = "".join(json.dumps(item) + "\n" for item in items)
            (root / "sessions" / "one.jsonl").write_text(content, encoding="utf-8")
            (root / "archived_sessions" / "copy.jsonl").write_text(content, encoding="utf-8")
            events, runs, stats = collect_usage_data(root, SHANGHAI, progress=False)
            self.assertTrue(events.empty)
            self.assertEqual(len(runs), 1)
            self.assertEqual(stats.runtime.duplicate_intervals_removed, 1)
            _, _, without_archived = collect_usage_data(root, SHANGHAI, False, False)
            self.assertEqual(without_archived.files_seen, 1)
            output = root / "report"
            with redirect_stdout(io.StringIO()):
                result = main([
                    "--codex-home", str(root), "--output", str(output), "--quiet",
                    "--cache-file", str(root / "cache.sqlite3"),
                    "--timezone", "Asia/Shanghai", "--start", "2026-08-28", "--end", "2026-08-30",
                ])
            self.assertEqual(result, 0)
            daily = pd.read_csv(output / "daily_running_time.csv")
            self.assertEqual(daily["running_hours"].tolist(), [0, 1, 0])
            self.assertEqual(daily["summed_running_hours"].tolist(), [0, 1, 0])
            combined = pd.read_csv(output / "daily_usage.csv")
            self.assertEqual(combined["running_hours"].tolist(), [0, 1, 0])
            self.assertEqual(combined["summed_running_hours"].tolist(), [0, 1, 0])
            self.assertEqual(combined["total_tokens"].sum(), 0)
            self.assertTrue(combined["cache_miss_percent"].isna().all())
            self.assertTrue(combined["rolling_7d_cache_miss_percent"].isna().all())
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertIsNone(summary["cache_miss_percent"])
            self.assertIn("11_daily_cache_miss_rate.png", summary["charts"])
            self.assertTrue((output / "11_daily_cache_miss_rate.png").is_file())
            self.assertIn("12_recent_hourly_tokens.png", summary["charts"])
            self.assertTrue((output / "12_recent_hourly_tokens.png").is_file())
            hourly_path = output / "recent_hourly_usage.csv"
            self.assertTrue(hourly_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            hourly = pd.read_csv(hourly_path)
            self.assertEqual(len(hourly), 168)
            self.assertEqual(hourly.loc[0, "hour_start"], "2026-08-24T00:00:00+08:00")
            self.assertEqual(hourly.loc[167, "hour_start"], "2026-08-30T23:00:00+08:00")
            self.assertEqual(int(hourly["total_tokens"].sum()), 0)
            self.assertEqual(summary["running_time"]["total_running_hours"], 1)
            self.assertEqual(summary["running_time"]["total_summed_running_hours"], 1)
            self.assertTrue((output / "10_daily_running_time.png").is_file())
            self.assertNotIn("PRIVATE CONTENT", (output / "running_intervals.csv").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
