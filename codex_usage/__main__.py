"""Lightweight command router; help and environment setup need no third-party packages."""

from __future__ import annotations

import argparse
from importlib import import_module
import sys


COMMANDS = {
    "gui": "gui",
    "report": "charts",
    "animate": "animations",
    "cost": "cost",
    "demo": "demo",
    "setup": "environment",
    "check": "environment",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m codex_usage",
        description="Codex 本地用量统计：图表、运行时间和 API 费用估算。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用命令：\n"
            "  python -m codex_usage gui       打开中文窗口\n"
            "  python -m codex_usage report    生成用量图片与数据文件\n"
            "  python -m codex_usage animate   按编号从已有数据文件生成 MP4 / GIF 动画\n"
            "  python -m codex_usage cost      根据 events.csv 估算费用\n"
            "  python -m codex_usage demo      使用合成数据体验图片生成\n"
            "  python -m codex_usage setup     准备本项目的 .venv 环境\n"
            "  python -m codex_usage check     只检查环境\n\n"
            "例如：python -m codex_usage report --timezone Asia/Shanghai --incremental\n"
            "查看子命令参数：python -m codex_usage report --help"
        ),
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, help="要执行的操作")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    arguments = ["--check", *args.arguments] if args.command == "check" else args.arguments
    try:
        module = import_module(f".{COMMANDS[args.command]}", __package__)
        return module.main(arguments) or 0
    except ModuleNotFoundError as exc:
        print(f"错误：缺少运行依赖（{exc.name}）。", file=sys.stderr)
        print(
            "请双击「启动用量统计.cmd」，或运行 python -m codex_usage setup，"
            "然后使用 .venv 中的 Python 重试。", file=sys.stderr,
        )
        if exc.name == "tkinter":
            print("Tkinter 需要通过 Python 安装程序或系统包管理器补装 Tcl/Tk。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
