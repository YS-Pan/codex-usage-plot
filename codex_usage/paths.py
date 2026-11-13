"""Resolve writable paths independently of the source and installed package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def config_file() -> Path:
    configured = os.environ.get("CODEX_USAGE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (root / "codex-usage/paths.json").expanduser().resolve()


def load_config() -> dict[str, str]:
    try:
        value = json.loads(config_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or set(value) - {"state_dir", "report_dir"}:
        raise ValueError(f"路径配置格式无效：{config_file()}")
    if any(not isinstance(path, str) or not path.strip() for path in value.values()):
        raise ValueError(f"路径配置必须使用非空路径：{config_file()}")
    return value


def configure(*, state: Path | None = None, reports: Path | None = None) -> None:
    value = load_config()
    if state is not None:
        value["state_dir"] = str(state.expanduser().resolve())
    if reports is not None:
        value["report_dir"] = str(reports.expanduser().resolve())
    target = config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            staged = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        staged.replace(target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def state_dir() -> Path:
    configured = os.environ.get("CODEX_USAGE_STATE_DIR") or load_config().get("state_dir")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return (root / "codex-usage").expanduser().resolve()


def report_dir() -> Path:
    configured = os.environ.get("CODEX_USAGE_REPORT_DIR") or load_config().get("report_dir")
    return Path(configured).expanduser().resolve() if configured else state_dir() / "reports"


def settings_file() -> Path:
    return state_dir() / "codex_usage_settings.json"


def report_cache_file() -> Path:
    return state_dir() / "cache/codex_usage_cache.sqlite3"


def query_cache_file() -> Path:
    return state_dir() / "cache/codex_usage_query_cache.sqlite3"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m codex_usage paths", description="显示运行目录，不创建文件。").parse_args(argv)
    print(json.dumps({
        "config_file": str(config_file()),
        "state": str(state_dir()), "reports": str(report_dir()),
        "settings": str(settings_file()), "report_cache": str(report_cache_file()),
        "query_cache": str(query_cache_file()),
    }, ensure_ascii=False, indent=2))
    return 0
