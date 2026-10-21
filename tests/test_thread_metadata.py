"""Task labels follow saved projects and original session dates before filtering."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import pandas as pd

from codex_usage import charts
from codex_usage.thread_metadata import directory_name, enrich_thread_metadata


TASK_TEMP = Path(__file__).resolve().parents[1] / ".task-tmp"


class ThreadMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TASK_TEMP.mkdir(exist_ok=True)

    def test_saved_names_assignments_and_longest_root_win_over_child_workspaces(self):
        state = {
            "local-projects": {
                "broad": {"name": "Parent project", "rootPaths": ["D:\\work"]},
                "nested": {"name": "Custom project name", "rootPaths": ["D:\\work\\nested"]},
            },
            "thread-project-assignments": {"assigned": {"projectId": "nested"}},
            "projectless-thread-ids": ["projectless"],
        }
        frame = pd.DataFrame([
            {"thread_id": "main", "session_id": "child", "cwd": "D:\\elsewhere"},
            {"thread_id": "assigned", "session_id": "assigned", "cwd": "D:\\unrelated"},
            {"thread_id": "matched", "session_id": "matched", "cwd": "d:/WORK/nested/subfolder"},
            {"thread_id": "boundary", "session_id": "boundary", "cwd": "D:\\work-other"},
            {"thread_id": "projectless", "session_id": "projectless", "cwd": "D:\\work"},
            {"thread_id": "unix", "session_id": "unix", "cwd": "/work/research"},
            {"thread_id": "missing", "session_id": "missing", "cwd": ""},
        ])
        frame["timestamp"] = pd.Timestamp("2026-10-08", tz="Asia/Shanghai")
        metadata = {"main": {"cwd": "D:\\work\\nested", "started_at": "2026-10-01T16:30:00Z"}}
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            home = Path(directory)
            (home / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
            actual = enrich_thread_metadata(frame, home, metadata).set_index("thread_id")
        self.assertEqual(actual["project_name"].to_dict(), {
            "main": "Custom project name", "assigned": "Custom project name", "matched": "Custom project name",
            "boundary": "work-other", "projectless": "No project", "unix": "research", "missing": "No project",
        })
        self.assertEqual(actual.loc["main", "thread_started_at"].isoformat(), "2026-10-02T00:30:00+08:00")
        self.assertEqual(directory_name("\\\\?\\D:\\work\\nested"), "nested")

    def test_missing_or_invalid_project_state_falls_back_to_main_directory(self):
        frame = pd.DataFrame([
            {"thread_id": "main", "session_id": "child", "cwd": "/work/child"},
            {"thread_id": "main", "session_id": "main", "cwd": "/work/root"},
        ])
        frame["timestamp"] = pd.date_range("2026-10-07", periods=2, tz="UTC")
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            home = Path(directory)
            for value in [None, "{", "[]", '{"local-projects": [], "thread-project-assignments": [], "projectless-thread-ids": null}']:
                with self.subTest(value=value):
                    if value is not None:
                        (home / ".codex-global-state.json").write_text(value, encoding="utf-8")
                    actual = enrich_thread_metadata(frame, home, {})
                    self.assertEqual(actual["project_name"].tolist(), ["root", "root"])
                    self.assertEqual(actual["thread_started_at"].tolist(), [frame.iloc[0]["timestamp"]] * 2)

    def test_header_only_root_date_survives_filter_cache_reuse_and_project_rename(self):
        zone = ZoneInfo("Asia/Shanghai")
        with tempfile.TemporaryDirectory(dir=TASK_TEMP) as directory:
            home = Path(directory)
            sessions = home / "sessions"
            sessions.mkdir()
            (sessions / "root.jsonl").write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-10-05T00:00:00Z",
                "payload": {"id": "root", "timestamp": "2026-10-01T16:30:00Z", "cwd": "D:\\work\\root"},
            }) + "\n", encoding="utf-8")
            records = [
                {"type": "session_meta", "timestamp": "2026-10-07T00:00:00Z",
                 "payload": {"id": "child", "parent_thread_id": "root", "cwd": "D:\\work\\child"}},
                {"type": "turn_context", "timestamp": "2026-10-07T00:00:00Z", "payload": {"model": "gpt-6.1-sol"}},
            ]
            records.extend({
                "type": "event_msg", "timestamp": stamp,
                "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "total_tokens": 100}}},
            } for stamp in ["2026-10-08T00:00:00Z", "2026-10-10T00:00:00Z"])
            (sessions / "child.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            state = {"local-projects": {"project": {"name": "Original name", "rootPaths": ["D:\\work\\root"]}}}
            state_path = home / ".codex-global-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            full, _, _ = charts.collect_usage_data(home, zone, progress=False)
            cache = home / "usage.sqlite3"
            cached, _, _ = charts.collect_usage_data(home, zone, progress=False, cache_path=cache, incremental=True)
            pd.testing.assert_frame_equal(full, cached)
            state["local-projects"]["project"]["name"] = "Renamed project"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            reused, _, stats = charts.collect_usage_data(home, zone, progress=False, cache_path=cache, incremental=True)
            self.assertEqual(stats.scan.bytes_read, 0)
            filtered = charts.filter_date_range(reused, pd.Timestamp("2026-10-10").date(), None)
            task = charts.aggregate_threads(filtered).iloc[0]
            self.assertEqual(task["project_name"], "Renamed project")
            self.assertEqual(task["thread_started_at"].isoformat(), "2026-10-02T00:30:00+08:00")
            self.assertEqual(task["first_seen"].date().isoformat(), "2026-10-10")

    def test_identical_two_line_labels_keep_separate_task_bars(self):
        from tests.test_thread_costs import events

        frame = events([{"thread_id": "a", "input_tokens": 100}, {"thread_id": "b", "input_tokens": 200}])
        frame["project_name"] = "Same project"
        frame["thread_started_at"] = pd.Timestamp("2026-10-01", tz="Asia/Shanghai")
        with patch("codex_usage.charts.save_figure", return_value=Path("06.png")) as saved:
            charts.plot_top_threads(charts.aggregate_threads(frame), frame, 5, Path("."), "png", 80)
        figure = saved.call_args.args[0]
        self.addCleanup(plt.close, figure)
        axis = figure.axes[0]
        self.assertEqual([text.get_text() for text in axis.get_yticklabels()], ["Same project · 2026-10-01\nSynthetic task"] * 2)
        self.assertEqual([bar.get_y() + bar.get_height() / 2 for bar in axis.containers[0]], [0, 1])


if __name__ == "__main__":
    unittest.main()
