"""Public commands keep argument forwarding and resolved paths stable."""

from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from codex_usage.__main__ import main
from codex_usage.charts import build_parser
from codex_usage import environment, gui, paths


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def test_help_and_setup_help_work_without_installed_dependencies(self):
        for arguments in ([], ["--help"], ["setup", "--help"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-S", "-m", "codex_usage", *arguments], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("python -m codex_usage", result.stdout)

    def test_report_options_are_forwarded_unchanged(self):
        handler = MagicMock()
        handler.main.return_value = 0
        arguments = ["--codex-home", "folder with spaces", "--timezone", "UTC", "--format", "svg", "--no-include-archived"]
        with patch("codex_usage.__main__.import_module", return_value=handler) as imported:
            self.assertEqual(main(["report", *arguments]), 0)
        imported.assert_called_once_with(".charts", "codex_usage")
        handler.main.assert_called_once_with(arguments)

    def test_check_command_is_read_only(self):
        handler = MagicMock()
        handler.main.return_value = 0
        with patch("codex_usage.__main__.import_module", return_value=handler):
            self.assertEqual(main(["check"]), 0)
        handler.main.assert_called_once_with(["--check"])

    def test_missing_dependency_has_install_instructions(self):
        with patch("codex_usage.__main__.import_module", side_effect=ModuleNotFoundError(name="pandas")), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["report"]), 1)
        self.assertIn("pandas", errors.getvalue())
        self.assertIn("codex_usage setup", errors.getvalue())

    def test_default_paths_and_explicit_overrides_survive(self):
        self.assertEqual(gui.REPORT_DIR, paths.report_dir())
        self.assertEqual(gui.SETTINGS_FILE, paths.settings_file())
        self.assertEqual(build_parser().parse_args([]).cache_file, paths.report_cache_file())
        with patch.dict(os.environ, {"CODEX_USAGE_STATE_DIR": str(ROOT / "state with spaces"),
                                     "CODEX_USAGE_REPORT_DIR": str(ROOT / "reports with spaces")}):
            defaults = build_parser().parse_args([])
            self.assertEqual(defaults.output, ROOT / "reports with spaces")
            self.assertEqual(defaults.cache_file, ROOT / "state with spaces/cache/codex_usage_cache.sqlite3")
            explicit = build_parser().parse_args(["--output", "chosen", "--cache-file", "chosen.sqlite3"])
            self.assertEqual(explicit.output, Path("chosen"))
            self.assertEqual(explicit.cache_file, Path("chosen.sqlite3"))


if __name__ == "__main__":
    unittest.main()
