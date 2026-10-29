"""Exercise child-output forwarding and Tk state without a desktop."""

from dataclasses import asdict
import io
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from codex_usage import gui
from codex_usage.progress import PROGRESS_ENV, PROGRESS_PREFIX, ProgressEvent


def record(event):
    return PROGRESS_PREFIX + json.dumps(asdict(event), ensure_ascii=False) + "\n"


class GuiProgressTests(unittest.TestCase):
    def test_tail_waits_for_complete_utf8_line_and_ignores_other_output(self):
        event = ProgressEvent("scan", "扫描日志", 1, 2, "个文件")
        encoded = record(event).encode("utf-8")
        split = encoded.index("扫".encode("utf-8")) + 1
        stream = io.BytesIO()
        reader = gui.ProgressLogReader(stream)
        messages = queue.Queue()
        stream.write(b"ordinary output\n" + encoded[:split])
        stream.seek(0)
        reader.forward(messages)
        self.assertTrue(messages.empty())
        position = stream.tell()
        stream.write(encoded[split:])
        stream.seek(position)
        reader.forward(messages)
        self.assertEqual(messages.get_nowait(), ("progress", event))
        reader.forward(messages)
        self.assertTrue(messages.empty())

    def test_worker_forwards_final_record_before_done_and_keeps_readable_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions").mkdir()
            event = ProgressEvent("images", "生成图片", 1, 1, "张")
            process = MagicMock()
            process.__enter__.return_value = process
            process.wait.return_value = 0

            def start(*args, **kwargs):
                self.assertEqual(kwargs["env"][PROGRESS_ENV], "json")
                kwargs["stdout"].write(record(event))
                kwargs["stdout"].flush()
                return process

            messages = queue.Queue()
            with patch.object(gui, "SETTINGS_FILE", root / "settings.json"), \
                    patch.object(gui.subprocess, "Popen", side_effect=start):
                gui.generate_reports(root / "output", messages, threading.Event(),
                                     options=gui.ReportOptions(root))
            items = list(messages.queue)
            self.assertEqual([value for kind, value in items if kind == "progress"], [event, event])
            self.assertEqual(items[-1][0], "done")
            self.assertIn("生成图片", (root / "output" / gui.LOG_NAME).read_text(encoding="utf-8"))

    def test_idle_child_can_be_cancelled_and_killed_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions").mkdir()
            process = MagicMock()
            process.__enter__.return_value = process
            cancelled = threading.Event()

            def wait(timeout=None):
                if timeout is not None:
                    cancelled.set()
                    raise subprocess.TimeoutExpired("report", timeout)
                return -1

            process.wait.side_effect = wait
            messages = queue.Queue()
            with patch.object(gui, "SETTINGS_FILE", root / "settings.json"), \
                    patch.object(gui.subprocess, "Popen", return_value=process) as popen:
                gui.generate_reports(root / "output", messages, cancelled, options=gui.ReportOptions(root))
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(list(messages.queue)[-1][0], "cancelled")

    def fake_app(self):
        app = gui.UsageApp.__new__(gui.UsageApp)
        app.messages = queue.Queue()
        app.cancel = threading.Event()
        app.closing = False
        app.worker = object()
        app.started_at = app.stage_started_at = 0
        app.progress_stage = None
        app.progress_mode = "indeterminate"
        for name in ("root", "status", "progress_detail", "elapsed", "progress", "activity",
                     "update_button", "cancel_button", "resize_to_content"):
            setattr(app, name, MagicMock())
        app.settings_widgets = [MagicMock()]
        return app

    def test_determinate_counts_reset_to_activity_for_unmeasured_stage(self):
        app = self.fake_app()
        app.show_progress(ProgressEvent("images", "图片", 2, 4, "张"))
        self.assertEqual(app.progress_mode, "determinate")
        app.progress.configure.assert_any_call(value=50)
        app.show_progress(ProgressEvent("saving", "保存动画"))
        self.assertEqual(app.progress_mode, "indeterminate")
        app.progress.start.assert_called_once_with(12)
        app.progress_detail.set.assert_called_with("")

    def test_cancel_status_is_not_overwritten_and_elapsed_continues_while_idle(self):
        app = self.fake_app()
        app.cancel.set()
        app.messages.put(("progress", ProgressEvent("images", "图片", 1, 4)))
        with patch.object(gui, "monotonic", return_value=65):
            app.poll_messages()
        app.status.set.assert_not_called()
        app.elapsed.set.assert_called_once_with("已用 01:05 · 当前阶段 01:05")

    def test_terminal_error_restores_controls_without_claiming_completion(self):
        app = self.fake_app()
        app.messages.put(("error", "用量图片已更新，但费用估算失败。\n详细信息"))
        with patch.object(gui.messagebox, "showerror") as error, patch.object(gui, "monotonic", return_value=12):
            app.poll_messages()
        self.assertIsNone(app.worker)
        app.progress.stop.assert_called_once()
        app.activity.grid_remove.assert_called_once()
        app.update_button.configure.assert_called_once_with(state="normal", text="更新图片")
        app.settings_widgets[0].state.assert_called_once_with(["!disabled"])
        self.assertIn("费用估算失败", app.status.set.call_args.args[0])
        error.assert_called_once()
