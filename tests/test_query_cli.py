"""The data-only CLI keeps stdout readable by other projects and protects inputs."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_usage.__main__ import main as route
from codex_usage.query_cli import TABLE_COLUMNS, _stdout_utf8, export_query_result, main


ROOT = Path(__file__).resolve().parents[1]


class QueryCommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.result = {
            "schema_version": 1,
            "filters": {"codex_home": str(self.home)},
            "summary": {"total_tokens": 30},
            "events": [{"session_id": "child", "total_tokens": 30}],
            "sessions": [{"session_id": "child", "models": ["模型", "gpt-6-luna"]}],
            "turns": [], "running_intervals": [],
            "diagnostics": {"complete": True, "unmatched_ids": {}},
        }

    def invoke(self, arguments, result=None, error=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch("codex_usage.query_cli._query_usage", return_value=self.result if result is None else result, side_effect=error) as queried, redirect_stdout(output), redirect_stderr(errors):
            code = main(["--codex-home", str(self.home), *arguments])
        return code, output.getvalue(), errors.getvalue(), queried

    def test_help_without_dependencies_and_route(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "codex_usage", "query", "--help"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--session-id", result.stdout)
        handler = unittest.mock.MagicMock()
        handler.main.return_value = 0
        with patch("codex_usage.__main__.import_module", return_value=handler) as imported:
            self.assertEqual(route(["query", "--session", "child"]), 0)
        imported.assert_called_once_with(".query_cli", "codex_usage")
        handler.main.assert_called_once_with(["--session", "child"])

    def test_json_stdout_and_default_no_cache_or_output(self):
        code, output, errors, queried = self.invoke([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), self.result)
        self.assertEqual(errors, "")
        self.assertIsNone(queried.call_args.kwargs["cache_path"])
        self.assertFalse(queried.call_args.kwargs["incremental"])
        self.assertEqual(queried.call_args.kwargs["timezone"], "UTC")
        self.assertEqual(list(self.root.iterdir()), [self.home])

    def test_filters_and_json_manifest_merge(self):
        manifest = self.root / "identities.json"
        manifest.write_text(json.dumps({"session_ids": ["child", "child2"], "turn_ids": ["t1"]}), encoding="utf-8-sig")
        code, _output, _errors, queried = self.invoke([
            "--ids-file", str(manifest), "--session", "child", "--session-id", "child3", "child2",
            "--thread", "root", "--model", "gpt-6-luna", "--include-descendants", "--no-include-archived",
            "--start", "2026-10-01T12:00:00+08:00", "--end", "2026-10-01T12:10:00+08:00",
            "--include-cost", "--quiet",
        ])
        self.assertEqual(code, 0)
        options = queried.call_args.kwargs
        self.assertEqual(options["session_ids"], ["child", "child3", "child2"])
        self.assertEqual(options["thread_ids"], ["root"])
        self.assertEqual(options["turn_ids"], ["t1"])
        self.assertEqual(options["models"], ["gpt-6-luna"])
        self.assertTrue(options["include_descendants"])
        self.assertTrue(options["include_cost"])
        self.assertFalse(options["include_archived"])
        self.assertFalse(options["progress"])

    def test_manifest_rejects_bad_shape_and_unknown_keys(self):
        manifest = self.root / "bad.json"
        for value in ([], {"session_ids": "child"}, {"session_ids": [True]}, {"session_ids": [""]}, {"unexpected": []}):
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value), encoding="utf-8")
                code, output, errors, queried = self.invoke(["--ids-file", str(manifest)])
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("错误", errors)
                queried.assert_not_called()

    def test_missing_manifest_is_io_error(self):
        code, output, errors, queried = self.invoke(["--ids-file", str(self.root / "missing.json")])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("错误", errors)
        queried.assert_not_called()

    def test_incremental_cache_is_separate_from_report_cache(self):
        code, _output, _errors, queried = self.invoke(["--incremental"])
        self.assertEqual(code, 0)
        self.assertEqual(queried.call_args.kwargs["cache_path"], ROOT / "codex_usage_query_cache.sqlite3")

    def test_output_contains_json_and_bom_csv_with_nested_values(self):
        destination = self.root / "output"
        code, output, errors, _queried = self.invoke(["--output", str(destination)])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), self.result)
        self.assertEqual(json.loads((destination / "usage.json").read_text(encoding="utf-8")), self.result)
        self.assertEqual({path.name for path in destination.iterdir()}, {"usage.json", "events.csv", "sessions.csv", "turns.csv", "running_intervals.csv"})
        for name in ("events", "sessions", "turns", "running_intervals"):
            self.assertTrue((destination / f"{name}.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        with (destination / "sessions.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(json.loads(row["models"]), ["模型", "gpt-6-luna"])

    def test_csv_stdout_has_bom_and_no_status_messages(self):
        code, output, errors, _queried = self.invoke(["--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertTrue(output.startswith("\ufeff"))
        rows = list(csv.DictReader(io.StringIO(output.removeprefix("\ufeff"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "child")
        self.assertEqual(rows[0]["total_tokens"], "30")
        self.assertEqual(list(rows[0]), TABLE_COLUMNS["events"])

    def test_partial_result_returns_failure_but_preserves_json(self):
        self.result["diagnostics"]["complete"] = False
        code, output, errors, _queried = self.invoke([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), self.result)
        self.assertEqual(errors, "")

    def test_no_match_zero_result_and_strict_exit_code(self):
        for table in ("sessions", "turns", "events", "running_intervals"):
            self.result[table] = []
        self.assertEqual(self.invoke([])[0], 0)
        code, output, _errors, _queried = self.invoke(["--require-match"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output), self.result)

    def test_unmatched_explicit_id_strict_exit_code(self):
        self.result["diagnostics"]["unmatched_ids"] = {"session_ids": ["missing"]}
        self.assertEqual(self.invoke(["--require-match"])[0], 3)
        self.assertEqual(self.invoke([])[0], 0)

    def test_existing_but_filtered_out_id_strict_exit_code(self):
        self.result["diagnostics"]["filtered_out_ids"] = {"session_ids": ["excluded"]}
        self.assertEqual(self.invoke(["--require-match"])[0], 3)
        self.assertEqual(self.invoke([])[0], 0)

    def test_cache_cannot_overwrite_manifest_before_query(self):
        manifest = self.root / "identities.json"
        manifest.write_text('{"session_ids": ["child"]}', encoding="utf-8")
        code, output, errors, queried = self.invoke(["--ids-file", str(manifest), "--cache-file", str(manifest)])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("调用清单", errors)
        queried.assert_not_called()
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), {"session_ids": ["child"]})

    def test_export_cannot_overwrite_query_cache(self):
        destination = self.root / "output"
        destination.mkdir()
        cache = destination / "events.csv"
        cache.write_bytes(b"existing cache")
        self.result["filters"]["cache_path"] = str(cache)
        with self.assertRaisesRegex(ValueError, "cache-file"):
            export_query_result(self.result, destination)
        self.assertEqual(cache.read_bytes(), b"existing cache")
        self.assertFalse((destination / "usage.json").exists())

    def test_empty_csv_exports_keep_full_schema_headers(self):
        from codex_usage.query import EVENT_COLUMNS

        self.assertEqual(TABLE_COLUMNS["events"], EVENT_COLUMNS)
        for table in ("events", "sessions", "turns", "running_intervals"):
            self.result[table] = []
        destination = self.root / "empty-output"
        export_query_result(self.result, destination)
        for table, columns in TABLE_COLUMNS.items():
            with self.subTest(table=table), (destination / f"{table}.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = csv.DictReader(handle)
                self.assertEqual(rows.fieldnames, columns)
                self.assertEqual(list(rows), [])
        self.assertIn("summed_running_seconds", TABLE_COLUMNS["sessions"])
        self.assertIn("cache_write_input_tokens", TABLE_COLUMNS["turns"])
        self.assertIn("observed_end", TABLE_COLUMNS["running_intervals"])

    def test_stdout_writes_utf8_bytes_with_ascii_text_stream(self):
        raw = io.BytesIO()
        text_stream = io.TextIOWrapper(raw, encoding="ascii")
        with patch("sys.stdout", text_stream):
            _stdout_utf8('\ufeff模型,用量\r\n')
        self.assertEqual(raw.getvalue(), '\ufeff模型,用量\r\n'.encode("utf-8"))
        text_stream.detach()

    def test_parameter_and_io_errors_stay_on_stderr(self):
        for error, expected in ((ValueError("未知时区"), 2), (OSError("权限不足"), 1)):
            with self.subTest(error=error):
                code, output, errors, _queried = self.invoke([], error=error)
                self.assertEqual(code, expected)
                self.assertEqual(output, "")
                self.assertIn(str(error), errors)
                self.assertNotIn("Traceback", errors)

    def test_output_cannot_overwrite_raw_logs_or_manifest(self):
        for output in (self.home, self.home / "sessions", self.home / "sessions" / "nested", self.home / "archived_sessions"):
            with self.subTest(output=output):
                code, stdout, _errors, queried = self.invoke(["--output", str(output)])
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                queried.assert_not_called()
        destination = self.root / "output"
        destination.mkdir()
        manifest = destination / "usage.json"
        manifest.write_text("{}", encoding="utf-8")
        code, _stdout, _errors, queried = self.invoke(["--ids-file", str(manifest), "--output", str(destination)])
        self.assertEqual(code, 2)
        queried.assert_not_called()
        self.assertEqual(manifest.read_text(encoding="utf-8"), "{}")

    def test_export_rejects_nonfinite_json_before_creating_output(self):
        self.result["summary"]["ratio"] = float("nan")
        destination = self.root / "bad-output"
        with self.assertRaises(ValueError):
            export_query_result(self.result, destination)
        self.assertFalse(destination.exists())

    def test_subprocess_queries_synthetic_logs_without_rendering_images(self):
        folder = self.home / "sessions"
        folder.mkdir()
        items = [
            {"type": "session_meta", "timestamp": "2026-10-20T00:00:00Z", "payload": {"id": "child", "parent_thread_id": "root"}},
            {"type": "event_msg", "timestamp": "2026-10-20T00:00:00Z", "payload": {"type": "task_started", "turn_id": "t1"}},
            {"type": "turn_context", "timestamp": "2026-10-20T00:00:00Z", "payload": {"turn_id": "t1", "model": "gpt-6-luna", "effort": "max"}},
            {"type": "event_msg", "timestamp": "2026-10-20T00:00:30Z", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 110}}}},
            {"type": "event_msg", "timestamp": "2026-10-20T00:01:00Z", "payload": {"type": "task_complete", "turn_id": "t1"}},
        ]
        (folder / "child.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
        destination = self.root / "synthetic-query"
        result = subprocess.run(
            [sys.executable, "-m", "codex_usage", "query", "--codex-home", str(self.home), "--session", "child", "--model", "gpt-6-luna", "--output", str(destination), "--quiet", "--require-match"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["summary"]["total_tokens"], 110)
        self.assertEqual(data["summary"]["usage_event_count"], 1)
        self.assertEqual(data["summary"]["running_seconds"], 60)
        self.assertEqual(data["events"][0]["session_id"], "child")
        self.assertEqual(data["events"][0]["reasoning_effort"], "max")
        self.assertEqual(len(list(destination.iterdir())), 5)
        self.assertFalse(any(path.suffix in (".png", ".svg", ".pdf", ".mp4", ".gif") for path in destination.iterdir()))
        for table, columns in TABLE_COLUMNS.items():
            with self.subTest(table=table):
                self.assertEqual(list(data[table][0]), columns)


if __name__ == "__main__":
    unittest.main()
