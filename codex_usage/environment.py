"""Explicit preparation and read-only checks for the current Python installation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from . import paths

DATA_CHECK = """
import numpy, pandas
import sys
if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd
from zoneinfo import ZoneInfo
ZoneInfo('Asia/Shanghai')
"""


def check_environment(python: Path, *, feature: str = "animation", gui: bool = False) -> subprocess.CompletedProcess:
    code = DATA_CHECK
    if feature in {"plotting", "animation"}:
        code += "\nimport matplotlib, PIL\n"
    if feature == "animation":
        code += "\nfrom codex_usage.encoders import ffmpeg_executable\nimport subprocess\nsubprocess.run([ffmpeg_executable(), '-version'], check=True, stdout=subprocess.DEVNULL)\n"
    if gui:
        code += "\nimport tkinter\n"
    return subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def install_target(extra: str, wheel: Path | None = None) -> str:
    """Prepare extras from installed metadata unless the caller selects a wheel."""
    if wheel is not None:
        wheel = wheel.expanduser().resolve()
        if not wheel.is_file() or wheel.suffix != ".whl":
            raise ValueError(f"安装包不存在：{wheel}；请用 --wheel 指定可用的 wheel。")
        return f"{wheel}[{extra}]"
    return f"codex-usage-plot[{extra}]=={__version__}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m codex_usage setup",
        description="检查当前 Python；仅 --install 显式安装可选依赖，不创建虚拟环境或下载编码器。",
    )
    parser.add_argument("--check", action="store_true", help="只检查（check 命令的兼容参数）")
    parser.add_argument("--feature", choices=("data", "plotting", "animation"), default=None, help="检查功能，默认 animation，含数据、图片与 FFmpeg")
    parser.add_argument("--gui", action="store_true", help="另检查 Tkinter，不创建窗口")
    parser.add_argument("--install", choices=("data", "plotting"), help="用当前 Python 的 pip 显式安装所选可选依赖")
    parser.add_argument("--wheel", type=Path, help="显式选择安装依赖时使用的本地 wheel；默认使用已装同版本包的元数据")
    parser.add_argument("--launch", action="store_true", help="检查成功后打开 GUI，不隐式安装依赖")
    parser.add_argument("--state-dir", type=Path, help="保存用户选择的状态目录，供之后的独立命令复用")
    parser.add_argument("--report-dir", type=Path, help="保存用户选择的默认报告目录")
    args = parser.parse_args(argv)
    if args.check and (args.install or args.launch or args.state_dir or args.report_dir):
        parser.error("--check 不能与安装、启动或保存路径参数一起使用。")
    if args.wheel and not args.install:
        parser.error("--wheel 需要同时指定 --install。")
    try:
        if args.state_dir is not None or args.report_dir is not None:
            paths.configure(state=args.state_dir, reports=args.report_dir)
            print(f"运行路径已保存：{paths.config_file()}")
            if not (args.install or args.feature or args.gui or args.launch):
                return 0
        if args.install:
            target = install_target(args.install, args.wheel)
            subprocess.run([sys.executable, "-m", "pip", "install", target], check=True)
        feature = args.feature or args.install or "animation"
        result = check_environment(Path(sys.executable), feature=feature, gui=args.gui or args.launch)
        print(f"检查环境：{sys.executable}")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            extra = "data" if feature == "data" else "plotting"
            print(f"可显式准备依赖：python -m codex_usage setup --install {extra}；FFmpeg/Tkinter 由系统安装。", file=sys.stderr)
            return 1
        print(f"环境可用：{feature}" + ("，Tkinter 可导入" if args.gui or args.launch else ""))
        if args.launch:
            python = Path(sys.executable)
            if os.name == "nt" and python.with_name("pythonw.exe").is_file():
                python = python.with_name("pythonw.exe")
            subprocess.Popen(
                [str(python), "-m", "codex_usage", "gui"], stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"环境准备未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
