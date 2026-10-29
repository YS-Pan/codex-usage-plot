"""Progress must describe completed work without inventing a total or ETA."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
import os
import unittest
from unittest.mock import patch

from codex_usage.progress import (
    PROGRESS_ENV, PROGRESS_PREFIX, ProgressEvent, ProgressReporter,
    format_elapsed, parse_progress, report_progress,
)


class ProgressTests(unittest.TestCase):
    def test_structured_events_use_stderr_and_unknown_totals_have_no_percent(self):
        with patch.dict(os.environ, {PROGRESS_ENV: "json"}), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            report_progress("aggregate", "正在汇总数据")
        self.assertEqual(stdout.getvalue(), "")
        event = parse_progress(stderr.getvalue())
        self.assertEqual(event.label, "正在汇总数据")
        self.assertIsNone(event.percent)
        self.assertEqual(event.counts, "")
        self.assertIsNone(ProgressEvent("scan", "扫描", 0, 0).percent)

    def test_count_is_stage_local_and_does_not_round_early_to_100_percent(self):
        event = ProgressEvent("scan", "扫描", 999, 1000, "个文件")
        self.assertIn("本阶段 99%", event.counts)
        self.assertNotIn("100%", event.counts)
        self.assertEqual(ProgressEvent("scan", "扫描", 1000, 1000).percent, 100)

    def test_throttling_preserves_first_last_and_forced_events(self):
        with patch.dict(os.environ, {PROGRESS_ENV: "json"}), \
                patch("codex_usage.progress.monotonic", side_effect=[0, 0.05, 0.1, 0.15]), \
                redirect_stderr(io.StringIO()) as stderr:
            reporter = ProgressReporter("scan", "扫描", total=3)
            reporter.update(0)
            reporter.update(1)
            reporter.update(2, force=True)
            reporter.update(3)
        self.assertEqual([parse_progress(line).completed for line in stderr.getvalue().splitlines()], [0, 2, 3])

    def test_quiet_produces_no_progress(self):
        with redirect_stderr(io.StringIO()) as stderr:
            ProgressReporter("scan", "扫描", total=1, enabled=False).update(1)
            report_progress("aggregate", "汇总", enabled=False)
        self.assertEqual(stderr.getvalue(), "")

    def test_regular_cli_progress_is_readable(self):
        with patch.dict(os.environ, {PROGRESS_ENV: ""}), redirect_stderr(io.StringIO()) as stderr:
            ProgressReporter("scan", "扫描", total=2, unit="个文件").update(1, "复用 1")
        self.assertIn("扫描 · 1/2 个文件（本阶段 50%） · 复用 1", stderr.getvalue())
        self.assertNotIn(PROGRESS_PREFIX, stderr.getvalue())

    def test_malformed_records_are_ignored(self):
        valid = asdict(ProgressEvent("scan", "扫描", 1, 2))
        for value in (None, [], 5, {**valid, "total": -1}, {**valid, "completed": 3},
                      {**valid, "completed": True}, {**valid, "total": None},
                      {**valid, "label": []}, {**valid, "stage": ""}, {**valid, "extra": 1}):
            with self.subTest(value=value):
                self.assertIsNone(parse_progress(PROGRESS_PREFIX + json.dumps(value)))
        self.assertIsNone(parse_progress("ordinary log line"))
        self.assertIsNone(parse_progress(PROGRESS_PREFIX + "{"))

    def test_elapsed_time_does_not_wrap_after_an_hour(self):
        self.assertEqual(format_elapsed(65.9), "01:05")
        self.assertEqual(format_elapsed(3661), "1:01:01")
        self.assertEqual(format_elapsed(-1), "00:00")
