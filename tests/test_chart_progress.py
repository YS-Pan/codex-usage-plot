"""Progress describes completed work, including cache and empty-data paths."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_usage import animations, charts, cost
from codex_usage.progress import PROGRESS_ENV, parse_progress


class ChartProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.output = self.root / "output"
        self.cache = self.root / "cache.sqlite3"
        self.zone = ZoneInfo("UTC")
        self.day = date(2026, 8, 29)
        rows = [
            {"type": "session_meta", "timestamp": "2026-08-29T01:00:00Z",
             "payload": {"id": "synthetic-session", "originator": "Codex Desktop"}},
            {"type": "turn_context", "timestamp": "2026-08-29T01:00:00Z",
             "payload": {"turn_id": "turn-1", "model": "gpt-6.1-sol"}},
            {"type": "event_msg", "timestamp": "2026-08-29T01:00:00Z",
             "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"type": "event_msg", "timestamp": "2026-08-29T01:01:00Z",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 100, "cached_input_tokens": 20,
                 "output_tokens": 10, "total_tokens": 110}}}},
            {"type": "event_msg", "timestamp": "2026-08-29T01:02:00Z",
             "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        ]
        self.log = self.home / "sessions" / "rollout-synthetic.jsonl"
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {PROGRESS_ENV: "json"}).start()

    @staticmethod
    def progress_events(output):
        return [event for line in output.getvalue().splitlines() if (event := parse_progress(line))]

    def test_single_file_and_cached_reuse_report_completed_files(self):
        for expected_detail in ("新增 1", "缓存复用 1"):
            with self.subTest(expected_detail=expected_detail), redirect_stderr(io.StringIO()) as output:
                charts.collect_usage_data(self.home, self.zone, cache_path=self.cache, incremental=True)
            events = self.progress_events(output)
            self.assertEqual(events[0].stage, "discover")
            scan = [event for event in events if event.stage == "scan"]
            self.assertEqual([(event.completed, event.total) for event in scan], [(0, 1), (1, 1)])
            self.assertIn(expected_detail, scan[-1].detail)
            self.assertEqual(events[-1].stage, "organize")

    def test_empty_scan_and_quiet_mode_do_not_invent_percentages(self):
        self.log.unlink()
        with redirect_stderr(io.StringIO()) as output:
            charts.collect_usage_data(self.home, self.zone)
        scan = [event for event in self.progress_events(output) if event.stage == "scan"]
        self.assertEqual(len(scan), 1)
        self.assertEqual((scan[0].completed, scan[0].total), (0, 0))
        self.assertIsNone(scan[0].percent)
        with redirect_stderr(io.StringIO()) as output:
            charts.collect_usage_data(self.home, self.zone, progress=False, cache_path=self.cache)
        self.assertEqual(output.getvalue(), "")

    def test_failed_cached_read_is_not_counted_as_completed(self):
        def fail_read(path, home, zone, titles, stats, *args, **kwargs):
            stats.files_failed += 1
            return iter(())

        with redirect_stderr(io.StringIO()) as output, \
                patch.object(charts, "parse_rollout", side_effect=fail_read), \
                self.assertRaisesRegex(OSError, "未推进缓存进度"):
            charts.collect_usage_data(self.home, self.zone, cache_path=self.cache)
        scan = [event for event in self.progress_events(output) if event.stage == "scan"]
        self.assertEqual([event.completed for event in scan], [0])

    def test_uncached_read_reports_failed_file_count(self):
        def fail_read(path, home, zone, titles, stats, *args, **kwargs):
            stats.files_failed += 1
            return iter(())

        with redirect_stderr(io.StringIO()) as output, patch.object(charts, "parse_rollout", side_effect=fail_read):
            charts.collect_usage_data(self.home, self.zone)
        scan = [event for event in self.progress_events(output) if event.stage == "scan"]
        self.assertEqual(scan[-1].completed, 1)
        self.assertIn("读取失败 1 个文件", scan[-1].detail)

    def mock_plots(self, stack):
        names = ("plot_daily_usage", "plot_cumulative_usage", "plot_daily_composition",
                 "plot_weekly_breakdown", "plot_activity_heatmap", "plot_top_threads",
                 "plot_request_sizes_over_time", "plot_daily_running_time",
                 "plot_daily_cache_miss_rate", "plot_recent_hourly_usage")
        for name in names:
            stack.enter_context(patch.object(charts, name, return_value=self.output / f"{name}.png"))
        stack.enter_context(patch.object(cost, "plot_recent_hourly_cost", return_value=self.output / "13.png"))
        stack.enter_context(patch.object(charts, "configure_matplotlib"))
        return stack.enter_context(patch.object(animations, "export_animations", return_value=[]))

    def test_picture_total_matches_data_and_advances_only_after_save(self):
        events, intervals, stats = charts.collect_usage_data(self.home, self.zone, progress=False)
        for empty, total in ((False, 12), (True, 4)):
            with self.subTest(empty=empty), ExitStack() as stack, redirect_stderr(io.StringIO()) as output:
                export_animations = self.mock_plots(stack)
                summary = charts.export_report(
                    events.iloc[:0] if empty else events, stats, self.output, self.zone, "png", 30, 3,
                    running_intervals=intervals, start=self.day, end=self.day,
                    animation=animations.AnimationOptions(charts=()),
                )
            emitted = self.progress_events(output)
            pictures = [event for event in emitted if event.stage == "charts"]
            saved = [event for event in pictures if event.detail.startswith("已保存")]
            self.assertEqual(pictures[0].completed, 0)
            self.assertEqual({event.total for event in pictures}, {total})
            self.assertEqual([event.completed for event in saved], list(range(1, total + 1)))
            self.assertEqual(len(summary["charts"]), total)
            self.assertEqual(emitted[-1].stage, "summary")
            self.assertTrue((self.output / "summary.json").is_file())
            self.assertTrue(export_animations.call_args.kwargs["progress"])

    def test_picture_failure_does_not_advance_or_start_later_stages(self):
        events, intervals, stats = charts.collect_usage_data(self.home, self.zone, progress=False)
        with ExitStack() as stack, redirect_stderr(io.StringIO()) as output:
            export_animations = self.mock_plots(stack)
            stack.enter_context(patch.object(charts, "plot_cumulative_usage", side_effect=OSError("save failed")))
            with self.assertRaisesRegex(OSError, "save failed"):
                charts.export_report(events, stats, self.output, self.zone, "png", 30, 3,
                                     running_intervals=intervals, start=self.day, end=self.day)
        emitted = self.progress_events(output)
        pictures = [event for event in emitted if event.stage == "charts"]
        self.assertEqual(max(event.completed for event in pictures), 1)
        self.assertNotIn("summary", [event.stage for event in emitted])
        export_animations.assert_not_called()

    def test_quiet_cli_suppresses_all_progress_and_passes_quiet_to_animation(self):
        with ExitStack() as stack, redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
            export_animations = self.mock_plots(stack)
            result = charts.main(["--codex-home", str(self.home), "--output", str(self.output),
                                  "--cache-file", str(self.cache), "--timezone", "UTC", "--quiet"])
        self.assertEqual(result, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse(export_animations.call_args.kwargs["progress"])


if __name__ == "__main__":
    unittest.main()
