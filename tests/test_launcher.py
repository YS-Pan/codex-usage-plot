"""Exercise the Windows batch entry point with an isolated synthetic module."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "启动用量统计.cmd"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")

PROBE = '''import json
import os
from pathlib import Path
import sys
import time

target = Path(os.environ["LAUNCHER_PROBE_DIR"])
stage = "preflight" if "--help" in sys.argv else "gui"
data = {
    "args": sys.argv[1:], "cwd": os.getcwd(), "python": sys.executable,
    "state": os.environ.get("CODEX_USAGE_STATE_DIR"),
    "reports": os.environ.get("CODEX_USAGE_REPORT_DIR"),
    "config": os.environ.get("CODEX_USAGE_CONFIG_FILE"),
}
(target / (stage + ".json")).write_text(json.dumps(data), encoding="utf-8")
if stage == "preflight":
    if os.environ.get("LAUNCHER_FAIL_PREFLIGHT"):
        print("synthetic missing Tk dependency", file=sys.stderr)
        raise SystemExit(7)
else:
    deadline = time.monotonic() + 15
    while not (target / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    (target / "exited").touch()
'''

CAPTURE = '''import json
import os
from pathlib import Path
Path(os.environ["LAUNCHER_PROBE_DIR"], "parent.json").write_text(json.dumps({
    "cwd": os.getcwd(),
    "state": os.environ.get("CODEX_USAGE_STATE_DIR"),
    "reports": os.environ.get("CODEX_USAGE_REPORT_DIR"),
    "config": os.environ.get("CODEX_USAGE_CONFIG_FILE"),
}), encoding="utf-8")
'''


@unittest.skipUnless(os.name == "nt" and PYTHONW.is_file(), "requires Windows Python with pythonw.exe")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.caller = self.root / "调用目录 caller"
        self.caller.mkdir()
        self.unrelated = self.root / "无关目录 temp"
        self.unrelated.mkdir()
        self.modules = self.root / "probe modules"
        package = self.modules / "codex_usage"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(PROBE, encoding="utf-8")
        self.capture = self.root / "capture.py"
        self.capture.write_text(CAPTURE, encoding="utf-8")
        self.environment = {**os.environ, "PYTHONPATH": str(self.modules), "PYTHONUTF8": "1",
                            "TEMP": str(self.unrelated), "CODEX_USAGE_PYTHON": sys.executable,
                            "CODEX_USAGE_STATE_DIR": "caller-state", "CODEX_USAGE_REPORT_DIR": "caller-reports",
                            "CODEX_USAGE_CONFIG_FILE": str(self.root / "caller-paths.json")}

    def run_launcher(self, name, *, fail=False, python=None):
        runtime = self.root / name
        runtime.mkdir()
        launcher = runtime / LAUNCHER.name
        shutil.copyfile(LAUNCHER, launcher)
        probe = runtime / "probe"
        probe.mkdir()
        wrapper = runtime / "caller.cmd"
        wrapper.write_text(
            '@echo off\nchcp 65001 >nul\n'
            f'call "{launcher}"\n'
            'set "LAUNCHER_RESULT=%ERRORLEVEL%"\n'
            f'"{sys.executable}" "{self.capture}"\n'
            'exit /b %LAUNCHER_RESULT%\n', encoding="utf-8",
        )
        environment = {**self.environment, "LAUNCHER_PROBE_DIR": str(probe)}
        if fail:
            environment["LAUNCHER_FAIL_PREFLIGHT"] = "1"
        if python is not None:
            environment["CODEX_USAGE_PYTHON"] = str(python)
        try:
            # A GUI may inherit output handles after cmd has exited. A file lets
            # us measure cmd's lifetime without waiting for those handles to close.
            output = probe / "cmd-output.txt"
            with output.open("wb") as stream:
                completed = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(wrapper)], cwd=self.caller,
                    env=environment, stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=10,
                )
            result = subprocess.CompletedProcess(
                completed.args, completed.returncode,
                output.read_text(encoding="utf-8", errors="replace"), "",
            )
            yield runtime, probe, result
        finally:
            (probe / "release").touch()
            deadline = time.monotonic() + 5
            while (probe / "gui.json").exists() and not (probe / "exited").exists() and time.monotonic() < deadline:
                time.sleep(0.05)

    def assert_parent_restored(self, probe):
        parent = json.loads((probe / "parent.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(parent["cwd"]), self.caller)
        self.assertEqual(parent["state"], "caller-state")
        self.assertEqual(parent["reports"], "caller-reports")
        self.assertEqual(parent["config"], self.environment["CODEX_USAGE_CONFIG_FILE"])

    def test_two_copies_use_their_own_roots_and_return_while_gui_lives(self):
        for name in ("运行一 with spaces", "运行二 with spaces"):
            with self.subTest(name=name):
                for runtime, probe, result in self.run_launcher(name):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    deadline = time.monotonic() + 5
                    while not (probe / "gui.json").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue((probe / "gui.json").is_file(), result.stdout + result.stderr)
                    self.assertFalse((probe / "exited").exists(), "launcher waited for GUI exit")
                    preflight = json.loads((probe / "preflight.json").read_text(encoding="utf-8"))
                    gui = json.loads((probe / "gui.json").read_text(encoding="utf-8"))
                    self.assertEqual(preflight["args"], ["gui", "--help"])
                    self.assertEqual(gui["args"], ["gui"])
                    self.assertEqual(Path(gui["python"]), PYTHONW)
                    for data in (preflight, gui):
                        self.assertEqual(Path(data["state"]), runtime)
                        self.assertEqual(Path(data["reports"]), runtime / "reports")
                        self.assertEqual(Path(data["cwd"]), self.unrelated)
                        self.assertEqual(data["config"], self.environment["CODEX_USAGE_CONFIG_FILE"])
                    self.assert_parent_restored(probe)

    def test_preflight_failure_retains_error_and_does_not_launch_gui(self):
        for runtime, probe, result in self.run_launcher("失败运行 with spaces", fail=True):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("synthetic missing Tk dependency", result.stdout + result.stderr)
            self.assertIn("check --gui", result.stdout + result.stderr)
            self.assertFalse((probe / "gui.json").exists())
            self.assert_parent_restored(probe)

    def test_missing_selected_python_is_actionable_and_restores_cwd(self):
        for runtime, probe, result in self.run_launcher("缺少解释器 with spaces", python=self.root / "missing.exe"):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing.exe", result.stdout + result.stderr)
            self.assertIn("check --gui", result.stdout + result.stderr)
            self.assertFalse((probe / "preflight.json").exists())
            self.assertFalse((probe / "gui.json").exists())
            self.assert_parent_restored(probe)

    def test_missing_sibling_pythonw_does_not_fall_back_to_another_python(self):
        interpreter_dir = self.root / "console only"
        interpreter_dir.mkdir()
        console = interpreter_dir / "python.exe"
        shutil.copyfile(sys.executable, console)
        self.environment["PYTHONHOME"] = sys.base_prefix
        self.environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        for runtime, probe, result in self.run_launcher("缺少pythonw with spaces", python=console):
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((probe / "preflight.json").exists(), result.stdout + result.stderr)
            self.assertIn(str(interpreter_dir / "pythonw.exe").lower(), (result.stdout + result.stderr).lower())
            self.assertFalse((probe / "gui.json").exists())
            self.assert_parent_restored(probe)


if __name__ == "__main__":
    unittest.main()
