"""Exercise newcomer workflows without a display, network or personal logs."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import codex_usage.gui as gui
from codex_usage.charts import collect_usage_data, main as usage_main
from codex_usage import demo
from codex_usage.cost import main as cost_main
from codex_usage import environment as setup


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "合成数据 with spaces"
        (self.home / "sessions").mkdir(parents=True)
        self.output = self.root / "图片 with spaces"

    def test_settings_restore_and_invalid_settings_use_environment_default(self):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"codex_home": str(self.home), "timezone": "UTC",
                                        "include_archived": False}), encoding="utf-8")
        self.assertEqual(gui.load_options(settings), gui.ReportOptions(self.home, "UTC", False))
        for invalid in ("{", "null", "[]", '{"codex_home": false}',
                        '{"codex_home":"x", "timezone":"UTC", "include_archived":"false"}'):
            settings.write_text(invalid, encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
                self.assertEqual(gui.load_options(settings), gui.ReportOptions(self.home))

    def test_archive_only_directory_and_invalid_zone_get_actionable_errors(self):
        (self.home / "sessions").rmdir()
        (self.home / "archived_sessions").mkdir()
        gui.validate_options(gui.ReportOptions(self.home))
        with self.assertRaisesRegex(ValueError, "包含归档"):
            gui.validate_options(gui.ReportOptions(self.home, include_archived=False))
        with self.assertRaisesRegex(ValueError, "时区"):
            gui.validate_options(gui.ReportOptions(self.home, "not/a/timezone"))
        with self.assertRaisesRegex(ValueError, "目录不存在"):
            gui.validate_options(gui.ReportOptions(self.home / "missing"))

    def test_worker_passes_selected_options_to_both_scripts_without_shell(self):
        options = gui.ReportOptions(self.home, "UTC", False)
        messages = queue.Queue()
        process = MagicMock()
        process.__enter__.return_value = process
        process.wait.return_value = 0
        settings = self.root / "settings.json"
        with patch.object(gui, "SETTINGS_FILE", settings), patch.object(gui.subprocess, "Popen", return_value=process) as popen:
            gui.generate_reports(self.output, messages, threading.Event(), True, options)
        self.assertEqual(popen.call_count, 2)
        first, second = [call.args[0] for call in popen.call_args_list]
        self.assertEqual(first[first.index("--codex-home") + 1], str(self.home))
        self.assertEqual(first[first.index("--timezone") + 1], "UTC")
        self.assertIn("--incremental", first)
        self.assertIn("--no-include-archived", first)
        self.assertEqual(second[second.index("--events") + 1], str(self.output / "events.csv"))
        self.assertTrue(all(not call.kwargs.get("shell", False) for call in popen.call_args_list))
        self.assertEqual(gui.load_options(settings), options)
        self.assertEqual(list(messages.queue)[-1][0], "done")

    def test_cancelled_worker_does_not_launch_a_report(self):
        cancelled = threading.Event()
        cancelled.set()
        messages = queue.Queue()
        with patch.object(gui, "SETTINGS_FILE", self.root / "settings.json"), patch.object(gui.subprocess, "Popen") as popen:
            gui.generate_reports(self.output, messages, cancelled, options=gui.ReportOptions(self.home))
        popen.assert_not_called()
        self.assertEqual(list(messages.queue)[-1][0], "cancelled")

    def test_worker_failure_stops_before_cost_and_explains_missing_dependency(self):
        messages = queue.Queue()
        process = MagicMock()
        process.__enter__.return_value = process
        process.wait.return_value = 1

        def fail(*args, **kwargs):
            kwargs["stdout"].write("ModuleNotFoundError: No module named 'pandas'\n")
            kwargs["stdout"].flush()
            return process

        with patch.object(gui, "SETTINGS_FILE", self.root / "settings.json"), patch.object(gui.subprocess, "Popen", side_effect=fail) as popen:
            gui.generate_reports(self.output, messages, threading.Event(), options=gui.ReportOptions(self.home))
        self.assertEqual(popen.call_count, 1)
        kind, text = list(messages.queue)[-1]
        self.assertEqual(kind, "error")
        self.assertIn("启动用量统计.cmd", text)
        self.assertTrue((self.output / gui.LOG_NAME).is_file())

    def test_cancel_during_scan_terminates_child_and_skips_cost(self):
        messages = queue.Queue()
        cancelled = threading.Event()
        process = MagicMock()
        process.__enter__.return_value = process

        def wait(timeout):
            if timeout == 0.2:
                cancelled.set()
                raise subprocess.TimeoutExpired("usage-report", timeout)
            return 0

        process.wait.side_effect = wait
        with patch.object(gui, "SETTINGS_FILE", self.root / "settings.json"), patch.object(gui.subprocess, "Popen", return_value=process) as popen:
            gui.generate_reports(self.output, messages, cancelled, options=gui.ReportOptions(self.home))
        process.terminate.assert_called_once()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(list(messages.queue)[-1][0], "cancelled")

    def test_setup_check_and_ready_environment_never_install(self):
        ready = subprocess.CompletedProcess([], 0, "", "")
        for argv in (["--check"], []):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), \
                    patch.object(setup, "ENV_PYTHON", Path(__file__)), \
                    patch.object(setup, "check_environment", return_value=ready), \
                    patch.object(setup.subprocess, "run") as run:
                self.assertEqual(setup.main(argv), 0)
                run.assert_not_called()

    def test_setup_install_failure_returns_error_without_launching(self):
        missing = subprocess.CompletedProcess([], 1, "", "missing dependency")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors, \
                patch.object(setup, "ENV_PYTHON", Path(__file__)), \
                patch.object(setup, "check_environment", return_value=missing), \
                patch.object(setup.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip")), \
                patch.object(setup.subprocess, "Popen") as popen:
            self.assertEqual(setup.main([]), 1)
        popen.assert_not_called()
        self.assertIn("环境准备未完成", errors.getvalue())

    def test_missing_data_and_cost_inputs_explain_next_step_without_output(self):
        with redirect_stderr(io.StringIO()) as errors:
            result = usage_main(["--codex-home", str(self.root / "missing"), "--output", str(self.output)])
        self.assertEqual(result, 2)
        self.assertIn("--codex-home", errors.getvalue())
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as failure:
            cost_main(["--events", str(self.root / "missing.csv"), "--output", str(self.output)])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("请先生成用量数据", errors.getvalue())
        self.assertFalse(self.output.exists())

    def test_demo_preserves_existing_non_demo_output(self):
        self.output.mkdir()
        report = self.output / "summary.json"
        report.write_text('{"keep": true}', encoding="utf-8")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            demo.main(["--output", str(self.output)])
        self.assertEqual(report.read_text(encoding="utf-8"), '{"keep": true}')
        self.assertFalse((self.output / "DEMO.txt").exists())

    def test_demo_is_reproducible_and_covers_idle_days_sources_and_subagents(self):
        demo_home = self.root / "demo"
        demo.write_demo_logs(demo_home)
        first = {path.name: path.read_bytes() for path in (demo_home / "sessions").iterdir()}
        other_home = self.root / "other"
        demo.write_demo_logs(other_home)
        self.assertEqual(first, {path.name: path.read_bytes() for path in (other_home / "sessions").iterdir()})
        events, intervals, stats = collect_usage_data(demo_home, ZoneInfo("Asia/Shanghai"), progress=False)
        self.assertEqual(len(events), 4 * 24 * 3 * 4)
        self.assertEqual(len(intervals), 4 * 24 * 3)
        self.assertEqual(events["thread_id"].nunique(), 2)
        self.assertEqual(events["timestamp"].dt.date.nunique(), 24)
        self.assertEqual(set(events["source"]), {"desktop", "cli", "subagent", "auto_review"})
        self.assertEqual(stats.files_failed, 0)
        self.assertEqual(set(events["cwd"]), {""})


if __name__ == "__main__":
    unittest.main()
