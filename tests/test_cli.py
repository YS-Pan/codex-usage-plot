"""Public commands keep argument forwarding and project-relative paths stable."""

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
from codex_usage import environment, gui


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

    def test_package_move_keeps_default_outputs_in_project_root(self):
        self.assertEqual(gui.APP_DIR, ROOT)
        self.assertEqual(environment.APP_DIR, ROOT)
        self.assertEqual(gui.REPORT_DIR, ROOT / "codex_usage_report")
        self.assertEqual(gui.SETTINGS_FILE, ROOT / "codex_usage_settings.json")
        self.assertEqual(build_parser().parse_args([]).cache_file, ROOT / "codex_usage_cache.sqlite3")


if __name__ == "__main__":
    unittest.main()
