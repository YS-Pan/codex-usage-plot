#!/usr/bin/env python3
"""A compact desktop app for updating local usage charts."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_DIR = Path(__file__).resolve().parents[1]
REPORT_DIR = APP_DIR / "codex_usage_report"
CACHE_FILE = APP_DIR / "codex_usage_cache.sqlite3"
LOG_NAME = "gui_run.log"
SETTINGS_FILE = APP_DIR / "codex_usage_settings.json"


@dataclass(frozen=True)
class ReportOptions:
    codex_home: Path
    timezone: str = "Asia/Shanghai"
    include_archived: bool = True


def load_options(path: Path = SETTINGS_FILE) -> ReportOptions:
    default = ReportOptions(Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return default
        home = value.get("codex_home")
        zone = value.get("timezone")
        archived = value.get("include_archived")
        if not isinstance(home, str) or not home.strip() or not isinstance(zone, str) or not zone.strip():
            return default
        if not isinstance(archived, bool):
            return default
        return ReportOptions(Path(home).expanduser(), zone, archived)
    except (OSError, ValueError):
        return default


def validate_options(options: ReportOptions) -> None:
    if not options.codex_home.is_dir():
        raise ValueError(f"数据目录不存在：\n{options.codex_home}\n\n请点击「浏览…」选择 Codex 数据目录。")
    folders = ["sessions", "archived_sessions"] if options.include_archived else ["sessions"]
    if not any((options.codex_home / folder).is_dir() for folder in folders):
        raise ValueError(
            "数据目录中没有可读取的会话文件夹。\n"
            "请选择包含 sessions 或 archived_sessions 的上一级目录（通常为 .codex），"
            "并检查是否需要勾选「包含归档」。\n\n"
            "尚无本地日志时，可以按使用指南中的演示步骤生成图片。"
        )
    try:
        ZoneInfo(options.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("时区无效。请选择 Asia/Shanghai、UTC，或填写有效的 IANA 时区名称。") from exc


def usage_arguments(output_dir: Path, options: ReportOptions, incremental: bool) -> list[str]:
    return [
        "--codex-home", str(options.codex_home), "--timezone", options.timezone,
        "--output", str(output_dir), "--cache-file", str(CACHE_FILE),
        "--include-archived" if options.include_archived else "--no-include-archived",
        *(["--incremental"] if incremental else []),
    ]


def worker_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.is_file():
            return str(console_python)
    return str(executable)


def generate_reports(
    output_dir: Path,
    messages: queue.Queue,
    cancel: threading.Event,
    incremental: bool = False,
    options: ReportOptions | None = None,
) -> None:
    """Run each CLI in order; communicate with Tk only through the queue."""
    phase = "准备输出目录"
    usage_finished = False
    log_path = output_dir / LOG_NAME
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        options = options or load_options()
        environment = os.environ.copy()
        environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        steps = [
            (
                "1/2 · 增量读取并生成图片与动画…" if incremental else "1/2 · 全部读取并生成图片与动画…",
                "report",
                usage_arguments(output_dir, options, incremental),
            ),
            (
                "2/2 · 正在生成费用估算…",
                "cost",
                ["--events", str(output_dir / "events.csv"), "--output", str(output_dir)],
            ),
        ]
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"Started: {datetime.now().isoformat(timespec='seconds')}\n")
            validate_options(options)
            try:
                settings = {**asdict(options), "codex_home": str(options.codex_home)}
                SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                log.write(f"未能保存界面设置，本次仍使用所选配置：{exc}\n")
            for phase, script, arguments in steps:
                if cancel.is_set():
                    messages.put(("cancelled", "更新已停止"))
                    return
                messages.put(("status", phase))
                log.write(f"\n{phase}\n")
                log.flush()
                with subprocess.Popen(
                    [worker_python(), "-u", "-m", "codex_usage", script, *arguments],
                    cwd=APP_DIR,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                ) as process:
                    while True:
                        if cancel.is_set():
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            messages.put(("cancelled", "更新已停止"))
                            return
                        try:
                            returncode = process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                if returncode:
                    raise RuntimeError(f"{script} 退出码：{returncode}")
                usage_finished = True
        mode = "增量读取" if incremental else "全部读取"
        messages.put(("done", f"{mode}完成 · {datetime.now():%H:%M}"))
    except Exception as exc:
        detail = str(exc)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n" + traceback.format_exc())
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
            if "ModuleNotFoundError" in tail:
                detail = "缺少运行依赖。请关闭窗口，重新双击「启动用量统计.cmd」，\n或运行 python -m codex_usage setup 后重试。"
            else:
                for line in reversed(tail.splitlines()):
                    if line.startswith(("错误：", "error:")):
                        detail = line
                        break
        except OSError:
            pass
        prefix = "用量图片已更新，但费用估算失败。" if usage_finished else "图片更新失败。"
        messages.put(("error", f"{prefix}\n\n{detail}\n\n详细记录：\n{log_path}"))


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    else:
        command = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([command, str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class UsageApp:
    def __init__(self, root: tk.Tk, output_dir: Path = REPORT_DIR) -> None:
        self.root = root
        self.output_dir = output_dir.resolve()
        self.messages: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self.settings_open = False
        options = load_options()
        self.codex_home = tk.StringVar(value=str(options.codex_home))
        self.timezone = tk.StringVar(value=options.timezone)
        self.include_archived = tk.BooleanVar(value=options.include_archived)

        root.title("Codex 用量统计")
        root.resizable(True, False)
        root.minsize(460, 0)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Action.TButton", padding=(12, 8))
        style.configure("Status.TLabel", foreground="#5c5c5c")

        frame = ttk.Frame(root, padding=14)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=0, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1, uniform="actions")
        actions.columnconfigure(1, weight=1, uniform="actions")
        self.update_button = ttk.Button(actions, text="更新图片", style="Action.TButton",
                                        command=self.start_update)
        self.update_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.folder_button = ttk.Button(actions, text="打开图片文件夹", style="Action.TButton",
                                        command=self.show_folder)
        self.folder_button.grid(row=0, column=1, sticky="ew")

        self.status = tk.StringVar(value=self.initial_status())
        ttk.Label(frame, textvariable=self.status, style="Status.TLabel", wraplength=430).grid(
            row=1, column=0, sticky="w", pady=(10, 8))
        self.activity = ttk.Frame(frame)
        self.activity.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.activity.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(self.activity, mode="indeterminate", length=300)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.cancel_button = ttk.Button(self.activity, text="停止", command=self.stop_update,
                                         state="disabled", width=7)
        self.cancel_button.grid(row=0, column=1)
        self.activity.grid_remove()

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=3, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        self.settings_toggle = ttk.Button(toolbar, text="设置 ▸", width=8, command=self.toggle_settings)
        self.settings_toggle.grid(row=0, column=0, sticky="w")
        ttk.Button(toolbar, text="运行日志", command=self.show_log, width=9).grid(
            row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="使用说明", command=lambda: self.open_result(APP_DIR / "docs" / "usage.md"),
                   width=9).grid(row=0, column=3)

        self.settings_panel = ttk.Frame(frame)
        self.settings_panel.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.settings_panel.columnconfigure(1, weight=1)
        ttk.Separator(self.settings_panel).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(self.settings_panel, text="数据目录").grid(row=1, column=0, padx=(0, 8))
        self.home_entry = ttk.Entry(self.settings_panel, textvariable=self.codex_home, width=30)
        self.home_entry.grid(row=1, column=1, sticky="ew")
        self.browse_button = ttk.Button(self.settings_panel, text="浏览…", command=self.choose_home, width=7)
        self.browse_button.grid(row=1, column=2, padx=(6, 0))
        ttk.Label(self.settings_panel, text="时区").grid(row=2, column=0, sticky="w", pady=10)
        self.timezone_entry = ttk.Combobox(
            self.settings_panel, textvariable=self.timezone,
            values=("Asia/Shanghai", "UTC", "America/New_York", "Europe/London"), width=23)
        self.timezone_entry.grid(row=2, column=1, sticky="w", pady=10)
        self.archive_check = ttk.Checkbutton(self.settings_panel, text="包含归档", variable=self.include_archived)
        self.archive_check.grid(row=2, column=2, sticky="w", padx=(6, 0))
        self.rebuild_button = ttk.Button(self.settings_panel, text="重新完整读取",
                                         command=lambda: self.start_update(incremental=False))
        self.rebuild_button.grid(row=3, column=0, columnspan=3, sticky="w")
        self.settings_widgets = [self.home_entry, self.browse_button, self.timezone_entry,
                                 self.archive_check, self.rebuild_button, self.settings_toggle]
        self.settings_panel.grid_remove()
        self.root.after(100, self.poll_messages)
        self.resize_to_content()

    def initial_status(self) -> str:
        try:
            updated = datetime.fromtimestamp((self.output_dir / "summary.json").stat().st_mtime)
            return f"上次更新：{updated:%Y-%m-%d %H:%M}"
        except OSError:
            return "尚未生成图片"

    def resize_to_content(self) -> None:
        self.root.update_idletasks()
        width = max(460, self.root.winfo_width(), self.root.winfo_reqwidth())
        self.root.geometry(f"{width}x{self.root.winfo_reqheight()}")

    def toggle_settings(self) -> None:
        self.set_settings_open(not self.settings_open)

    def set_settings_open(self, visible: bool) -> None:
        self.settings_open = visible
        if visible:
            self.settings_panel.grid()
        else:
            self.settings_panel.grid_remove()
        self.settings_toggle.configure(text="设置 ▾" if visible else "设置 ▸")
        self.resize_to_content()

    def choose_home(self) -> None:
        selected = filedialog.askdirectory(title="选择 Codex 数据目录（包含 sessions 的目录）", parent=self.root)
        if selected:
            self.codex_home.set(selected)

    def stop_update(self) -> None:
        if self.worker is not None:
            self.cancel.set()
            self.cancel_button.state(["disabled"])
            self.status.set("正在停止更新…")

    def show_log(self) -> None:
        path = self.output_dir / LOG_NAME
        if path.is_file():
            self.open_result(path)
        else:
            messagebox.showinfo("暂无运行日志", "生成图片后，可在这里查看进度和错误详情。", parent=self.root)

    def start_update(self, incremental: bool = True) -> None:
        if self.worker is not None:
            return
        if not self.codex_home.get().strip():
            self.set_settings_open(True)
            messagebox.showerror("请选择数据目录", "请填写数据目录，或点击「浏览…」。", parent=self.root)
            return
        try:
            options = ReportOptions(Path(self.codex_home.get().strip()).expanduser().resolve(),
                                    self.timezone.get().strip(), self.include_archived.get())
            validate_options(options)
        except (OSError, ValueError) as exc:
            self.set_settings_open(True)
            messagebox.showerror("请检查设置", str(exc), parent=self.root)
            return
        self.cancel.clear()
        self.update_button.configure(state="disabled", text="更新中…")
        self.cancel_button.state(["!disabled"])
        for widget in self.settings_widgets:
            widget.state(["disabled"])
        self.status.set("正在准备增量读取…" if incremental else "正在准备完整读取…")
        self.activity.grid()
        self.progress.start(12)
        self.resize_to_content()
        self.worker = threading.Thread(
            target=generate_reports,
            args=(self.output_dir, self.messages, self.cancel, incremental, options), daemon=True
        )
        self.worker.start()

    def poll_messages(self) -> None:
        try:
            while True:
                kind, text = self.messages.get_nowait()
                if kind == "status":
                    if not self.closing and not self.cancel.is_set():
                        self.status.set(text)
                    continue
                self.worker = None
                self.progress.stop()
                self.activity.grid_remove()
                if self.closing:
                    self.root.destroy()
                    return
                self.update_button.configure(state="normal", text="更新图片")
                self.cancel_button.state(["disabled"])
                for widget in self.settings_widgets:
                    widget.state(["!disabled"])
                self.status.set(text.splitlines()[0])
                self.resize_to_content()
                if kind == "error":
                    messagebox.showerror("更新未完成", text, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_messages)

    def show_folder(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)
            return
        self.open_result(self.output_dir)

    def open_result(self, path: Path) -> None:
        try:
            open_path(path)
        except OSError as exc:
            messagebox.showerror("无法打开", f"{path}\n\n{exc}", parent=self.root)

    def close(self) -> None:
        if self.closing:
            return
        if self.worker is None:
            self.root.destroy()
        elif messagebox.askyesno(
            "停止更新？", "正在生成图片。关闭窗口会停止更新，仍要退出吗？", parent=self.root
        ):
            self.closing = True
            self.status.set("正在停止更新…")
            self.cancel.set()


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(prog="python -m codex_usage gui", description="打开 Codex 本地用量统计窗口。").parse_args(argv)
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    UsageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
