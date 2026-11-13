"""Installed commands keep state separate and data queries need no plotting stack."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_usage import environment, paths


ROOT = Path(__file__).resolve().parents[1]


class InstalledPathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        selected = {
            "CODEX_USAGE_CONFIG_FILE": str(self.root / "config/paths.json"),
            "CODEX_USAGE_STATE_DIR": "", "CODEX_USAGE_REPORT_DIR": "",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        }
        self.configured = patch.dict(os.environ, selected)
        self.configured.start()
        self.addCleanup(self.configured.stop)

    def test_saved_paths_apply_to_next_process_without_state_environment(self):
        state, reports = self.root / "state with spaces", self.root / "chosen reports"
        with patch.object(environment, "check_environment") as check, redirect_stdout(io.StringIO()):
            self.assertEqual(environment.main(["--state-dir", str(state), "--report-dir", str(reports)]), 0)
        check.assert_not_called()
        self.assertFalse(state.exists())
        self.assertFalse(reports.exists())
        selected = subprocess.check_output(
            [sys.executable, "-m", "codex_usage", "paths"], cwd=ROOT,
            text=True, encoding="utf-8",
        )
        result = json.loads(selected)
        self.assertEqual(Path(result["state"]), state)
        self.assertEqual(Path(result["reports"]), reports)
        self.assertEqual(Path(result["query_cache"]), state / "cache/codex_usage_query_cache.sqlite3")
        with patch.dict(os.environ, {"CODEX_USAGE_STATE_DIR": str(self.root / "override")}):
            self.assertEqual(paths.state_dir(), self.root / "override")
            self.assertEqual(paths.report_dir(), reports)

    def test_query_including_cost_does_not_import_plotting_or_encoders(self):
        home = self.root / "synthetic home"
        (home / "sessions").mkdir(parents=True)
        records = [
            {"type": "session_meta", "timestamp": "2026-10-01T00:00:00Z", "payload": {"id": "synthetic"}},
            {"type": "turn_context", "timestamp": "2026-10-01T00:00:00Z", "payload": {"turn_id": "turn", "model": "gpt-5.5"}},
            {"type": "event_msg", "timestamp": "2026-10-01T00:00:01Z", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "total_tokens": 110}}}},
        ]
        (home / "sessions/synthetic.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        code = """
import importlib.abc, json, sys
class BlockPlotting(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'matplotlib', 'PIL', 'imageio_ffmpeg'}:
            raise AssertionError('query imported optional plotting library: ' + fullname)
sys.meta_path.insert(0, BlockPlotting())
from codex_usage.query import query_usage
result = query_usage(sys.argv[1], include_cost=True, progress=False)
print(json.dumps(result))
"""
        result = subprocess.run([sys.executable, "-c", code, str(home)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"]["total_tokens"], 110)
        self.assertFalse(paths.state_dir().exists())

    def test_default_extra_has_no_source_or_wheel_path_dependency(self):
        self.assertEqual(environment.install_target("data"), "codex-usage-plot[data]==0.2.0")

    def test_explicit_local_wheel_extra_uses_caller_selected_artifact(self):
        wheel = self.root / "codex_usage_plot-0.2.0-py3-none-any.whl"
        wheel.touch()
        self.assertEqual(environment.install_target("data", wheel), f"{wheel}[data]")


if __name__ == "__main__":
    unittest.main()
