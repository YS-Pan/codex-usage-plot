#!/usr/bin/env python3
"""Prepare the project's virtual environment using only the standard library."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


APP_DIR = Path(__file__).resolve().parents[1]
ENV_DIR = APP_DIR / ".venv"
ENV_PYTHON = ENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

# Import the packages as well as checking versions: a broken binary install can
# have correct package metadata but still fail when a report starts.
ENV_CHECK = """
import sys
assert sys.version_info >= (3, 10), '需要 Python 3.10 或更新版本'
import matplotlib, numpy, pandas
from importlib.metadata import version
for package, minimum in [('matplotlib', (3, 8)), ('numpy', (1, 26)), ('pandas', (2, 0))]:
    actual = tuple(int(part) for part in version(package).split('.')[:2])
    assert actual >= minimum, package + ' 版本过旧'
if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd
from zoneinfo import ZoneInfo
ZoneInfo('Asia/Shanghai')
"""


def check_environment(python: Path, *, gui: bool = False) -> subprocess.CompletedProcess:
    code = ENV_CHECK + ("\nimport tkinter\n" if gui else "")
    return subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codex_usage setup", description="创建或检查本项目的 .venv 环境。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--launch", action="store_true", help="准备完成后打开中文 GUI")
    mode.add_argument("--check", action="store_true", help="只检查环境，不创建、安装或启动")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        print("请安装 Python 3.10 或更新版本，并包含 Tcl/Tk。", file=sys.stderr)
        return 1
    try:
        if args.check:
            python = ENV_PYTHON if ENV_PYTHON.is_file() else Path(sys.executable)
            result = check_environment(python, gui=True)
            print(f"检查环境：{python}")
            print("环境可用，可以启动 GUI 或生成图片。" if result.returncode == 0 else result.stderr)
            return int(result.returncode != 0)

        if args.launch:
            # Tk belongs to Python, not pip; explain this before any installation.
            subprocess.run([sys.executable, "-c", "import tkinter"], check=True)
        if not ENV_PYTHON.is_file():
            if ENV_DIR.exists():
                raise RuntimeError(".venv 已存在但找不到 Python；请重命名该目录后重试。")
            print("正在创建本项目的 .venv 环境…", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(ENV_DIR)], check=True)
        print("正在检查运行依赖…", flush=True)
        result = check_environment(ENV_PYTHON, gui=args.launch)
        if result.returncode:
            print("首次安装或依赖不完整，正在安装 requirements.txt 中的依赖…", flush=True)
            subprocess.run(
                [str(ENV_PYTHON), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
                check=True,
            )
            result = check_environment(ENV_PYTHON, gui=args.launch)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
        print(f"环境已就绪：{ENV_PYTHON}", flush=True)
        if args.launch:
            python = ENV_PYTHON
            if os.name == "nt" and python.with_name("pythonw.exe").is_file():
                python = python.with_name("pythonw.exe")
            subprocess.Popen(
                [str(python), "-m", "codex_usage", "gui"], cwd=APP_DIR,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"\n环境准备未完成：{exc}", file=sys.stderr)
        print(
            "请检查上方错误与网络连接后重新运行。若提示 tkinter、venv 或 ensurepip 缺失，"
            "请补装 Python 的对应组件。更多说明见 docs/troubleshooting.md。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
