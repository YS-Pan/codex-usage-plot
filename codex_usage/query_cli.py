"""Machine-readable usage queries; importing help requires only the standard library."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any


ID_FIELDS = ("session_ids", "thread_ids", "turn_ids", "models")
TABLE_FIELDS = ("events", "sessions", "turns", "running_intervals")
TOKEN_FIELDS = [
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "uncached_input_tokens",
    "cached_input_component", "cache_write_input_component", "visible_output_tokens",
    "reasoning_output_component", "unclassified_tokens",
]
TOTAL_FIELDS = [
    *TOKEN_FIELDS, "usage_event_count", "cache_miss_percent", "running_seconds",
    "summed_running_seconds", "interval_count", "incomplete_interval_count",
]
# Keep standard-library-only help while exporting a fixed schema even with no rows.
TABLE_COLUMNS = {
    "events": ["timestamp", "session_id", "thread_id", "parent_thread_id", "turn_id", "model", "reasoning_effort", "source", *TOKEN_FIELDS],
    "sessions": ["session_id", "thread_id", "parent_thread_id", "parent_turn_id", "models", "reasoning_efforts", *TOTAL_FIELDS],
    "turns": ["session_id", "thread_id", "turn_id", "models", "reasoning_efforts", "statuses", *TOTAL_FIELDS],
    "running_intervals": ["session_id", "thread_id", "turn_id", "status", "timing_source", "models", "start", "end", "observed_start", "observed_end", "duration_seconds"],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_usage query",
        description="查询本机保留日志的 token 与运行时间数据，默认只向标准输出返回 JSON，不生成图片。",
        epilog=("同一参数的多个值取并集，不同参数之间取交集。时间范围为 [start, end)。"
                "会话使用完整 ID，模型使用日志中的完整名称。无匹配默认返回成功和零用量。"),
    )
    parser.add_argument("--codex-home", type=Path, help="Codex 数据目录，默认 CODEX_HOME 或 ~/.codex。")
    parser.add_argument("--session-id", "--session", dest="session_ids", action="extend", nargs="+", default=[], metavar="ID", help="实际会话 ID，可多值、重复指定；默认只统计该会话。")
    parser.add_argument("--thread-id", "--thread", dest="thread_ids", action="extend", nargs="+", default=[], metavar="ID", help="根任务 ID，包含该任务的所有子代理会话。")
    parser.add_argument("--turn-id", "--turn", dest="turn_ids", action="extend", nargs="+", default=[], metavar="ID", help="实际回合 ID，可多值、重复指定。")
    parser.add_argument("--model", dest="models", action="extend", nargs="+", default=[], metavar="NAME", help="日志中的模型名称，可多值、重复指定。")
    parser.add_argument("--include-descendants", action="store_true", help="同时统计 --session-id 指定会话的所有后代；不推测父回合归属。")
    parser.add_argument("--ids-file", type=Path, help="JSON 清单，可包含 session_ids/thread_ids/turn_ids/models 字符串数组，与命令行取并集。")
    parser.add_argument("--start", help="起始 ISO 时间，包含该时刻；无 UTC 偏移时按 --timezone 解释。")
    parser.add_argument("--end", help="结束 ISO 时间，不包含该时刻；无 UTC 偏移时按 --timezone 解释。")
    parser.add_argument("--timezone", default="UTC", help="输出及无偏移时间使用的 IANA 时区，默认 UTC。")
    parser.add_argument("--include-archived", action=argparse.BooleanOptionalAction, default=True, help="包含归档日志，默认包含。")
    parser.add_argument("--cache-file", type=Path, help="显式使用统计缓存；默认只读扫描，不创建缓存。")
    parser.add_argument("--incremental", action="store_true", help="增量读取；未指定缓存时使用工具目录中的 codex_usage_query_cache.sqlite3。")
    parser.add_argument("--quiet", action="store_true", help="隐藏写入标准错误的扫描进度。")
    parser.add_argument("--include-cost", action="store_true", help="附带按工具现有单价计算的 API 费用估算。")
    parser.add_argument("--require-match", action="store_true", help="没有匹配结果或指定 ID 未匹配时返回退出码 3，仍输出数据。")
    parser.add_argument("--format", choices=("json", "csv"), default="json", help="标准输出格式：JSON 或事件 CSV（UTF-8 BOM），默认 JSON。")
    parser.add_argument("--output", type=Path, help="另存 usage.json 及 events/sessions/turns/running_intervals.csv 数据文件的目录。")
    return parser


def _query_usage(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .query import query_usage

    return query_usage(*args, **kwargs)


def _load_ids(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    with path.expanduser().open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or set(value) - set(ID_FIELDS):
        raise ValueError("--ids-file 必须是仅包含 session_ids/thread_ids/turn_ids/models 的 JSON 对象。")
    for key, items in value.items():
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"--ids-file 的 {key} 必须是非空字符串数组。")
    return value


def _csv_text(rows: list[dict[str, Any]], table: str) -> str:
    columns = list(dict.fromkeys([*TABLE_COLUMNS[table], *(key for row in rows for key in row)]))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})
    return stream.getvalue()


def _validate_output(output_dir: Path, codex_home: Path, protected: tuple[Path, ...] = ()) -> None:
    home = codex_home.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    destinations = [output / "usage.json", *(output / f"{table}.csv" for table in TABLE_FIELDS)]
    candidates = [output, *(path.resolve() for path in destinations)]
    forbidden = [(home / root).resolve() for root in ("sessions", "archived_sessions")]
    for path in candidates:
        if path == home or path == home / "session_index.jsonl" or any(path == root or path.is_relative_to(root) for root in forbidden):
            raise ValueError("输出目录及数据文件不能覆盖 Codex 数据根目录或原始日志。")
    protected_paths = {path.expanduser().resolve() for path in protected}
    if protected_paths.intersection(path.resolve() for path in destinations):
        raise ValueError("输出数据文件不能覆盖 --ids-file 或 --cache-file。")


def _write_atomic(path: Path, content: str, *, encoding: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _stdout_utf8(content: str) -> None:
    """Keep piped data UTF-8 even when the Windows console uses another encoding."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(content)
    else:
        buffer.write(content.encode("utf-8"))
        buffer.flush()


def export_query_result(result: dict[str, Any], output_dir: Path) -> None:
    """Save one query as JSON plus BOM CSV tables; reject original-log destinations."""
    output_dir = Path(output_dir).expanduser().resolve()
    codex_home = result.get("filters", {}).get("codex_home")
    if not isinstance(codex_home, str) or not codex_home:
        raise ValueError("查询结果缺少 filters.codex_home，无法校验输出位置。")
    cache_path = result.get("filters", {}).get("cache_path")
    protected = (Path(cache_path),) if cache_path else ()
    _validate_output(output_dir, Path(codex_home), protected)
    # Serialize everything before opening output files, so invalid data writes nothing.
    serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    tables = {table: _csv_text(result.get(table, []), table) for table in TABLE_FIELDS}
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(output_dir / "usage.json", serialized, encoding="utf-8")
    for table, content in tables.items():
        _write_atomic(output_dir / f"{table}.csv", content, encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = (args.codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    try:
        ids = _load_ids(args.ids_file)
        selected = {key: list(dict.fromkeys([*getattr(args, key), *ids.get(key, [])])) for key in ID_FIELDS}
        cache_path = args.cache_file
        if args.incremental and cache_path is None:
            cache_path = Path(__file__).resolve().parents[1] / "codex_usage_query_cache.sqlite3"
        if cache_path is not None and args.ids_file is not None and cache_path.expanduser().resolve() == args.ids_file.expanduser().resolve():
            raise ValueError("--cache-file 不能覆盖 --ids-file 调用清单。")
        if args.output is not None:
            _validate_output(args.output, home, tuple(path for path in (args.ids_file, cache_path) if path is not None))
        result = _query_usage(
            home, **selected, include_descendants=args.include_descendants,
            start=args.start, end=args.end, timezone=args.timezone,
            include_archived=args.include_archived, cache_path=cache_path,
            incremental=args.incremental, progress=not args.quiet, include_cost=args.include_cost,
        )
    except (ValueError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"错误：读取用量数据失败：{exc}", file=sys.stderr)
        return 1

    try:
        if args.output is not None:
            export_query_result(result, args.output)
        if args.format == "json":
            _stdout_utf8(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            _stdout_utf8("\ufeff" + _csv_text(result.get("events", []), "events"))
    except (OSError, ValueError, TypeError) as exc:
        print(f"错误：导出用量数据失败：{exc}", file=sys.stderr)
        return 1

    diagnostics = result.get("diagnostics", {})
    if not diagnostics.get("complete", True):
        return 1
    if args.require_match and (
        not any(result.get(key) for key in TABLE_FIELDS)
        or any(diagnostics.get("unmatched_ids", {}).values())
        or any(diagnostics.get("filtered_out_ids", {}).values())
    ):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
