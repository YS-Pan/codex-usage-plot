#!/usr/bin/env python3
"""Build time-series charts from local Codex token and running-time metadata.

The parser intentionally ignores chat message bodies.  It reads only session
metadata, turn timing, and ``token_count`` events from rollout JSONL files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator, TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm, to_rgb
from matplotlib.font_manager import fontManager
from matplotlib.ticker import FuncFormatter, MaxNLocator, NullFormatter, PercentFormatter

from .cache import (
    CacheInvalidError,
    UsageCache,
    can_resume,
    checkpoint_hashes,
    file_identity,
)
from .running_time import (
    RunningInterval,
    RunningIntervalTracker,
    RuntimeParseStats,
    aggregate_daily_running_time,
    deduplicate_running_intervals,
    local_midnight,
    running_intervals_frame,
)
from .thread_metadata import directory_name, enrich_thread_metadata
from .progress import ProgressReporter, report_progress
from .model_prices import AUTO_REVIEW_MODEL

if TYPE_CHECKING:
    from .animations import AnimationOptions


_figure_sink: ContextVar[Callable[[plt.Figure], None] | None] = ContextVar("figure_sink", default=None)


@contextmanager
def figure_sink(sink: Callable[[plt.Figure], None]) -> Iterator[None]:
    """Reuse normal plots for a video frame without writing still images."""
    token = _figure_sink.set(sink)
    try:
        yield
    finally:
        _figure_sink.reset(token)


RAW_TOKEN_COLUMNS = [
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
]

COMPOSITION_COLUMNS = [
    "uncached_input_tokens",
    "cached_input_component",
    "cache_write_input_component",
    "visible_output_tokens",
    "reasoning_output_component",
    "unclassified_tokens",
]

COMPOSITION_LABELS = {
    "uncached_input_tokens": "Uncached input",
    "cached_input_component": "Cached input",
    "cache_write_input_component": "Cache-write input",
    "visible_output_tokens": "Visible output",
    "reasoning_output_component": "Reasoning output",
    "unclassified_tokens": "Unclassified",
}

PALETTE = [
    "#2563EB",
    "#14B8A6",
    "#F59E0B",
    "#EF4444",
    "#8B5CF6",
    "#64748B",
    "#EC4899",
    "#84CC16",
]


@dataclass
class ScanStats:
    mode: str = "full"
    files_reused: int = 0
    files_new: int = 0
    files_appended: int = 0
    files_rebuilt: int = 0
    files_moved: int = 0
    files_removed: int = 0
    bytes_read: int = 0
    seconds: float = 0.0


@dataclass
class ParseStats:
    files_seen: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    malformed_lines: int = 0
    token_events_seen: int = 0
    duplicate_events_removed: int = 0
    fallback_delta_events: int = 0
    empty_token_events: int = 0
    runtime: RuntimeParseStats = field(default_factory=RuntimeParseStats)
    scan: ScanStats | None = None


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def resolve_timezone(value: str) -> tzinfo:
    if value.lower() == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{value}；请使用 Asia/Shanghai、UTC 等 IANA 时区名称。") from exc


def parse_iso_timestamp(value: object, target_tz: tzinfo) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(target_tz)


def safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def token_values(payload: dict[str, Any]) -> dict[str, int]:
    values = {column: safe_int(payload.get(column)) for column in RAW_TOKEN_COLUMNS}
    if values["total_tokens"] == 0:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def subtract_usage(
    current: dict[str, int], previous: dict[str, int] | None
) -> dict[str, int]:
    if previous is None:
        return current.copy()
    delta: dict[str, int] = {}
    for column in RAW_TOKEN_COLUMNS:
        difference = current[column] - previous.get(column, 0)
        # A negative difference means the cumulative counter reset.  In that
        # case the current value is the safest local estimate for this event.
        delta[column] = difference if difference >= 0 else current[column]
    if delta["total_tokens"] == 0:
        delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
    return delta


def token_composition(values: dict[str, int]) -> dict[str, int]:
    """Convert overlapping counters into mutually exclusive chart segments."""
    total = values["total_tokens"]
    input_component = min(values["input_tokens"], total)
    remaining = max(total - input_component, 0)
    output_component = min(values["output_tokens"], remaining)

    cached = min(values["cached_input_tokens"], input_component)
    cache_write = min(
        values["cache_write_input_tokens"], max(input_component - cached, 0)
    )
    uncached = max(input_component - cached - cache_write, 0)

    reasoning = min(values["reasoning_output_tokens"], output_component)
    visible_output = max(output_component - reasoning, 0)
    unclassified = max(total - input_component - output_component, 0)

    return {
        "uncached_input_tokens": uncached,
        "cached_input_component": cached,
        "cache_write_input_component": cache_write,
        "visible_output_tokens": visible_output,
        "reasoning_output_component": reasoning,
        "unclassified_tokens": unclassified,
    }


def load_session_titles(codex_home: Path) -> dict[str, str]:
    index_path = codex_home / "session_index.jsonl"
    titles: dict[str, str] = {}
    if not index_path.is_file():
        return titles
    with index_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = item.get("id")
            title = item.get("thread_name")
            if isinstance(session_id, str) and isinstance(title, str) and title.strip():
                titles[session_id] = title.strip()
    return titles


def discover_rollouts(codex_home: Path, include_archived: bool) -> list[Path]:
    roots = [codex_home / "sessions"]
    if include_archived:
        roots.append(codex_home / "archived_sessions")
    files: set[Path] = set()
    for root in roots:
        if root.is_dir():
            for pattern in ("*.jsonl", "*.jsonl.zst"):
                files.update(path.resolve() for path in root.rglob(pattern) if path.is_file())
    return sorted(files, key=lambda path: str(path).lower())


def is_compressed_rollout(path: Path) -> bool:
    return path.name.endswith(".jsonl.zst")


def rollout_lines(
    path: Path, cursor: dict[str, Any], read_limit: int | None
) -> Iterable[bytes]:
    """Yield complete JSONL lines without materializing a decompressed log.

    Plain logs use logical byte checkpoints; compressed snapshots use physical
    file size only after every frame has been validated and consumed.
    """
    with path.open("rb") as handle:
        limit = read_limit if read_limit is not None else os.fstat(handle.fileno()).st_size
        if not is_compressed_rollout(path):
            position = cursor["offset"]
            handle.seek(position)
            while position < limit:
                line = handle.readline(limit - position)
                cursor["bytes_read"] += len(line)
                # Leave an actively written final record (including split UTF-8)
                # at its beginning for the next incremental read.
                if not line.endswith(b"\n"):
                    break
                position += len(line)
                cursor["offset"] = position
                yield line
            return

        try:
            from compression import zstd
        except ImportError:
            from backports import zstd

        if cursor["offset"]:
            raise ValueError("压缩日志必须从文件开头读取")
        decoder = zstd.ZstdDecompressor()
        remaining = limit
        pending = b""
        try:
            while remaining:
                block = handle.read(min(64 * 1024, remaining))
                if not block:
                    raise OSError(f"压缩日志在读取期间被截断：{path}")
                remaining -= len(block)
                cursor["bytes_read"] += len(block)
                while True:
                    if decoder.eof:
                        decoder = zstd.ZstdDecompressor()
                    # Bound output too: a tiny compressed block can expand to
                    # hundreds of MiB of repeated log data.
                    decoded = decoder.decompress(block, max_length=128 * 1024)
                    block = b""
                    lines = (pending + decoded).split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        yield line + b"\n"
                    if decoder.eof:
                        block = decoder.unused_data
                        if not block:
                            break
                    elif decoder.needs_input:
                        break
            if not decoder.eof:
                raise OSError(f"压缩日志不完整，未更新统计缓存：{path}")
        except zstd.ZstdError as exc:
            raise OSError(f"无法解压日志：{path}") from exc
        cursor["offset"] = limit


def first_nonempty(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def classify_source(meta: dict[str, Any], model: str) -> str:
    thread_source = first_nonempty(meta.get("thread_source")).lower()
    originator = first_nonempty(meta.get("originator")).lower()
    source = meta.get("source")
    source_text = json.dumps(source, ensure_ascii=False).lower() if source else ""
    model_lower = model.lower()

    if (
        "auto-review" in model_lower
        or "auto_review" in model_lower
        or "guardian" in thread_source
        or "guardian" in source_text
        or "auto_review" in thread_source
        or "auto-review" in thread_source
    ):
        return "auto_review"
    if meta.get("_source_category") in ("auto_review", "ambient", "subagent", "desktop", "cli", "other"):
        return meta["_source_category"]
    if "ambient" in thread_source or "ambient" in originator or "ambient" in source_text:
        return "ambient"
    if "subagent" in thread_source or "subagent" in source_text or "sub_agent" in thread_source:
        return "subagent"
    if "desktop" in originator:
        return "desktop"
    if "cli" in originator:
        return "cli"
    return "other"


def relative_log_path(path: Path, codex_home: Path) -> str:
    try:
        return str(path.relative_to(codex_home.resolve()))
    except ValueError:
        return str(path)


def session_parent_thread_id(meta: dict[str, Any]) -> str:
    """Read both current and older subagent parent metadata."""
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    nested_parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    direct_spawn = source.get("thread_spawn") if isinstance(source, dict) else None
    direct_parent = direct_spawn.get("parent_thread_id") if isinstance(direct_spawn, dict) else None
    return first_nonempty(nested_parent, direct_parent, meta.get("parent_thread_id"))


def populate_usage_inventory(
    inventory: dict[str, Any] | None,
    sessions: dict[str, dict[str, Any]],
    parents: dict[str, str],
    turns: Iterable[dict[str, Any]],
) -> None:
    """Expose metadata even for sessions and turns with no token events."""
    if inventory is None:
        return
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for turn in turns:
        key = (turn["session_id"], turn["turn_id"])
        record = merged.setdefault(key, {
            "session_id": key[0], "turn_id": key[1],
            "models": [], "reasoning_efforts": [],
        })
        for field_name in ("models", "reasoning_efforts"):
            record[field_name] = sorted(set(record[field_name]) | set(turn[field_name]))
    inventory["sessions"] = {
        session_id: {**meta, "parent_thread_id": parents.get(session_id, "")}
        for session_id, meta in sorted(sessions.items())
    }
    inventory["parents"] = dict(sorted(parents.items()))
    inventory["turns"] = [merged[key] for key in sorted(merged)]


def resolve_thread_roots(parent_threads: dict[str, str]) -> dict[str, str]:
    """Follow all parent links without recursion or looping on malformed cycles."""
    roots: dict[str, str] = {}
    for thread_id in parent_threads:
        current = thread_id
        path: list[str] = []
        positions: dict[str, int] = {}
        while current not in roots:
            if current in positions:
                # A cycle has no true root; choose one stable ID for the component.
                root = min(path[positions[current]:])
                break
            positions[current] = len(path)
            path.append(current)
            parent = parent_threads.get(current, "")
            if not parent or parent == current:
                root = current
                break
            current = parent
        else:
            root = roots[current]
        for member in path:
            roots[member] = root
    return roots


def choose_title(meta: dict[str, Any], titles: dict[str, str]) -> tuple[str, str, str]:
    session_id = first_nonempty(meta.get("id"), meta.get("session_id"))
    thread_id = first_nonempty(
        meta.get("parent_thread_id"), meta.get("session_id"), meta.get("id")
    )
    for candidate in (
        thread_id,
        first_nonempty(meta.get("session_id")),
        session_id,
        first_nonempty(meta.get("parent_thread_id")),
    ):
        if candidate and candidate in titles:
            return session_id, thread_id, titles[candidate]
    fallback = f"Untitled {thread_id[:8]}" if thread_id else "Untitled session"
    return session_id, thread_id or session_id, fallback


def parse_rollout(
    path: Path,
    codex_home: Path,
    target_tz: tzinfo,
    titles: dict[str, str],
    stats: ParseStats,
    runtime_tracker: RunningIntervalTracker | None = None,
    parent_threads: dict[str, str] | None = None,
    cursor: dict[str, Any] | None = None,
    read_limit: int | None = None,
) -> Iterable[tuple[dict[str, Any], tuple[object, ...]]]:
    cursor = cursor if cursor is not None else {}
    meta: dict[str, Any] = cursor.get("meta", {})
    current_turn_id = cursor.get("turn_id", "")
    current_model = cursor.get("model", "unknown")
    current_reasoning_effort = cursor.get("reasoning_effort", "")
    previous_total: dict[str, int] | None = cursor.get("previous_total")
    cursor.setdefault("offset", 0)
    cursor.setdefault("parents", {})
    turns = cursor.setdefault("turns", [])
    turn_records = {(turn["session_id"], turn["turn_id"]): turn for turn in turns}
    cursor["bytes_read"] = 0
    log_file = relative_log_path(path, codex_home)

    def register_turn(include_context: bool = True) -> None:
        if not current_turn_id:
            return
        session_id = first_nonempty(meta.get("id"), meta.get("session_id"))
        key = (session_id, current_turn_id)
        if key not in turn_records:
            turn_records[key] = {
                "session_id": session_id, "turn_id": current_turn_id,
                "models": [], "reasoning_efforts": [],
            }
            turns.append(turn_records[key])
        if include_context:
            for field_name, value in (("models", current_model),
                                      ("reasoning_efforts", current_reasoning_effort)):
                if value and (field_name != "models" or value != "unknown"):
                    values = turn_records[key][field_name]
                    if value not in values:
                        values.append(value)

    try:
        for line in rollout_lines(path, cursor, read_limit):
            try:
                item = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                stats.malformed_lines += 1
                continue

            if not isinstance(item, dict):
                stats.malformed_lines += 1
                continue

            event_type = item.get("type")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue

            if runtime_tracker is not None:
                runtime_tracker.observe(event_type, payload, item.get("timestamp"))

            if event_type == "session_meta":
                # Copy only non-content metadata.  Large instruction and
                # message fields are deliberately ignored.
                meta = {
                    key: payload.get(key)
                    for key in (
                        "id",
                        "session_id",
                        "parent_thread_id",
                        "cwd",
                        "originator",
                        "cli_version",
                        "source",
                        "thread_source",
                        "model_provider",
                    )
                }
                started = parse_iso_timestamp(payload.get("timestamp"), timezone.utc)
                if started is None:
                    started = parse_iso_timestamp(item.get("timestamp"), timezone.utc)
                meta["started_at"] = started.isoformat() if started is not None else None
                # Persist a source category and parent ID, never arbitrary
                # nested source fields that could contain message content.
                category = classify_source(meta, "unknown")
                source = meta.get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                meta["_source_category"] = category
                meta["source"] = {}
                direct_spawn = source.get("thread_spawn") if isinstance(source, dict) else None
                parent_turn_ids = []
                for container, spawn_metadata in (("subagent", spawn), ("", direct_spawn)):
                    if not isinstance(spawn_metadata, dict):
                        continue
                    allowed = {
                        key: value for key in ("parent_thread_id", "parent_turn_id")
                        if isinstance(value := spawn_metadata.get(key), str) and value.strip()
                    }
                    if allowed:
                        destination = meta["source"].setdefault(container, {}) if container else meta["source"]
                        destination["thread_spawn"] = allowed
                    parent_turn_ids.append(allowed.get("parent_turn_id"))
                meta["parent_turn_id"] = first_nonempty(*parent_turn_ids)
                # Register header-only sessions too: they can bridge a nested
                # subagent to its main thread even without any token events.
                session_id = first_nonempty(meta.get("id"), meta.get("session_id"))
                parent_id = session_parent_thread_id(meta)
                if session_id:
                    for parents in (parent_threads, cursor["parents"]):
                        if parents is not None and (session_id not in parents or parent_id):
                            parents[session_id] = parent_id
                continue

            if event_type == "turn_context":
                next_turn_id = first_nonempty(payload.get("turn_id"), current_turn_id)
                if next_turn_id != current_turn_id:
                    current_reasoning_effort = ""
                current_turn_id = next_turn_id
                current_model = first_nonempty(payload.get("model"), current_model) or "unknown"
                current_reasoning_effort = first_nonempty(
                    payload.get("effort"), payload.get("reasoning_effort"),
                )
                register_turn()
                continue

            payload_type = payload.get("type")
            if event_type == "event_msg" and payload_type == "task_started":
                next_turn_id = first_nonempty(payload.get("turn_id"), current_turn_id)
                if next_turn_id != current_turn_id:
                    current_reasoning_effort = ""
                current_turn_id = next_turn_id
                register_turn(include_context=False)
                continue
            if not (event_type == "event_msg" and payload_type == "token_count"):
                continue

            next_turn_id = first_nonempty(payload.get("turn_id"), current_turn_id)
            if next_turn_id != current_turn_id:
                current_reasoning_effort = ""
            current_turn_id = next_turn_id
            register_turn()
            stats.token_events_seen += 1
            info = payload.get("info")
            if not isinstance(info, dict):
                stats.empty_token_events += 1
                continue

            total_usage_payload = info.get("total_token_usage")
            total_usage = (
                token_values(total_usage_payload)
                if isinstance(total_usage_payload, dict)
                else None
            )
            last_usage_payload = info.get("last_token_usage")
            if isinstance(last_usage_payload, dict):
                values = token_values(last_usage_payload)
            elif total_usage is not None:
                values = subtract_usage(total_usage, previous_total)
                stats.fallback_delta_events += 1
            else:
                stats.empty_token_events += 1
                continue
            if total_usage is not None:
                previous_total = total_usage

            if values["total_tokens"] <= 0:
                stats.empty_token_events += 1
                continue

            timestamp = parse_iso_timestamp(item.get("timestamp"), target_tz)
            if timestamp is None:
                stats.malformed_lines += 1
                continue

            session_id, thread_id, title = choose_title(meta, titles)
            source_label = classify_source(meta, current_model)
            record: dict[str, Any] = {
                "timestamp": timestamp,
                "session_id": session_id,
                "thread_id": thread_id,
                "parent_thread_id": session_parent_thread_id(meta),
                "turn_id": current_turn_id,
                "title": title,
                "model": current_model,
                "reasoning_effort": current_reasoning_effort,
                "source": source_label,
                "originator": first_nonempty(meta.get("originator")) or "unknown",
                "cwd": first_nonempty(meta.get("cwd")),
                "log_file": log_file,
            }
            record.update(values)
            record.update(token_composition(values))

            # Forked or migrated rollouts can repeat the same completed
            # turn.  The turn/timestamp/counters fingerprint removes exact
            # copies without guessing about merely similar requests.
            identity = current_turn_id or thread_id or session_id or str(path)
            fingerprint = (
                identity,
                timestamp.isoformat(),
                current_model,
                *(values[column] for column in RAW_TOKEN_COLUMNS),
            )
            yield record, fingerprint
        stats.files_parsed += 1
    except OSError:
        stats.files_failed += 1
    finally:
        cursor.update(meta=meta, turn_id=current_turn_id, model=current_model,
                      reasoning_effort=current_reasoning_effort, previous_total=previous_total)


def frame_from_records(
    records: list[dict[str, Any]],
    parent_threads: dict[str, str],
    titles: dict[str, str],
    target_tz: tzinfo,
) -> pd.DataFrame:
    columns = [
        "timestamp", "session_id", "thread_id", "parent_thread_id", "turn_id",
        "title", "model", "reasoning_effort", "source", "originator", "cwd", "log_file",
        *RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS,
    ]
    frame = pd.DataFrame.from_records(records, columns=columns)
    frame["reasoning_effort"] = frame["reasoning_effort"].fillna("")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(target_tz)
    if not frame.empty:
        # Resolve after all headers and request deduplication, before date filters.
        roots = resolve_thread_roots(parent_threads)
        frame["thread_id"] = frame["thread_id"].map(roots).fillna(frame["thread_id"])
        root_titles = {
            root: titles.get(root, f"Untitled {root[:8]}" if root else "Untitled session")
            for root in frame["thread_id"].unique()
        }
        frame["title"] = frame["thread_id"].map(root_titles)
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    return frame


def restore_parse_stats(value: dict[str, Any]) -> ParseStats:
    try:
        counters = {key: item for key, item in value.items() if key not in ("runtime", "scan")}
        if any(not isinstance(item, int) or item < 0 for item in counters.values()):
            raise ValueError("Invalid parse counter")
        if not isinstance(value.get("runtime"), dict):
            raise ValueError("Invalid runtime statistics")
        if any(not isinstance(item, int) or item < 0 for item in value["runtime"].values()):
            raise ValueError("Invalid runtime counter")
        runtime = RuntimeParseStats(**value["runtime"])
        return ParseStats(**counters, runtime=runtime)
    except (KeyError, TypeError, ValueError) as exc:
        raise CacheInvalidError("Invalid cached parse statistics") from exc


def restore_cached_tracker(tracker: RunningIntervalTracker, cursor: dict[str, Any]) -> None:
    try:
        tracker.restore_checkpoint(cursor.get("runtime", {}))
    except (KeyError, TypeError, ValueError) as exc:
        raise CacheInvalidError("Invalid cached running-turn state") from exc


def collect_cached_usage_data(
    codex_home: Path,
    target_tz: tzinfo,
    include_archived: bool,
    progress: bool,
    cache_path: Path,
    incremental: bool,
    *,
    inventory: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, list[RunningInterval], ParseStats]:
    started = perf_counter()
    codex_home = codex_home.expanduser().resolve()
    cache_path = cache_path.expanduser().resolve()
    report_progress("discover", "正在查找日志…", enabled=progress)
    files = discover_rollouts(codex_home, include_archived)
    paths = {relative_log_path(path, codex_home): path for path in files}
    active = set(paths)
    if cache_path == codex_home / "session_index.jsonl" or any(
        cache_path.is_relative_to(codex_home / root) for root in ("sessions", "archived_sessions")
    ):
        raise ValueError("缓存文件不能覆盖 Codex 原始日志")
    scan = ScanStats(mode="incremental" if incremental else "full")
    stats = ParseStats(files_seen=len(files), scan=scan)
    reporter = ProgressReporter("scan", "正在检查日志", total=len(files), unit="个文件", enabled=progress)
    reporter.update(0)
    titles = load_session_titles(codex_home)
    with UsageCache(cache_path, codex_home) as cache:
        if not incremental:
            cache.clear()
        entries = cache.entries()
        # Archiving normally moves a file; preserve its cursor when file identity
        # and checkpoints still match. A copied rollout is parsed and deduplicated.
        moved_candidates = {
            entry["file_id"]: log for log, entry in entries.items()
            if log not in active and entry["file_id"]
        }
        for index, (log, path) in enumerate(paths.items(), start=1):
            stat = path.stat()
            entry = entries.get(log)
            if entry is None:
                old = moved_candidates.pop(file_identity(stat), None)
                if old is not None and can_resume(path, stat, entries[old]):
                    cache.move(old, log)
                    entry = entries.pop(old)
                    entry["log_file"] = log
                    entries[log] = entry
                    scan.files_moved += 1
            unchanged = entry is not None and (
                stat.st_size == entry["size"] and stat.st_mtime_ns == entry["mtime_ns"]
                and (not entry["file_id"] or file_identity(stat) == entry["file_id"])
            )
            if unchanged:
                scan.files_reused += 1
            else:
                # A compressed file's checkpoint is its physical size, never a
                # seek position in the decompressed JSONL. Rebuild on any change.
                if entry is not None and not is_compressed_rollout(path) and can_resume(path, stat, entry):
                    scan.files_appended += 1
                    cursor = entry["state"]
                    file_stats = restore_parse_stats(entry["stats"])
                    file_stats.files_parsed = file_stats.files_failed = 0
                else:
                    if entry is None:
                        scan.files_new += 1
                    else:
                        scan.files_rebuilt += 1
                        cache.remove(log)
                    cursor = {}
                    file_stats = ParseStats()
                tracker = RunningIntervalTracker(log, file_stats.runtime)
                restore_cached_tracker(tracker, cursor)
                new_events = list(parse_rollout(
                    path, codex_home, timezone.utc, {}, file_stats,
                    tracker, cursor=cursor, read_limit=stat.st_size,
                ))
                if file_stats.files_failed:
                    raise OSError(f"读取日志失败，未推进缓存进度：{path}")
                scan.bytes_read += cursor.pop("bytes_read")
                # Save the live turn before producing a temporary EOF interval.
                # Its completion on a later run will replace the EOF estimate.
                cursor["runtime"] = tracker.checkpoint()
                head, tail = checkpoint_hashes(path, cursor["offset"])
                entry = {
                    "log_file": log, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "file_id": file_identity(stat), "offset": cursor["offset"],
                    "head_hash": head, "tail_hash": tail,
                    "state": cursor, "stats": asdict(file_stats),
                }
                cache.save_file(log, entry)
                cache.save_events(log, new_events)
                cache.save_intervals(log, tracker.intervals)
                entries[log] = entry
            reporter.update(index, f"缓存复用 {scan.files_reused}，新增 {scan.files_new}，"
                            f"续读 {scan.files_appended}，重读 {scan.files_rebuilt}")

        report_progress("organize", "正在整理用量与运行时间…", enabled=progress)
        scanned_roots = {"sessions", "archived_sessions"} if include_archived else {"sessions"}
        for log in list(entries):
            if log not in active and Path(log).parts[0] in scanned_roots:
                cache.remove(log)
                del entries[log]
                scan.files_removed += 1

        parent_threads: dict[str, str] = {}
        session_metadata: dict[str, dict[str, Any]] = {}
        turns: list[dict[str, Any]] = []
        running_intervals = list(cache.load_intervals(active))
        for log in paths:
            entry = entries[log]
            meta = entry["state"].get("meta", {})
            session_id = first_nonempty(meta.get("id"), meta.get("session_id"))
            if session_id:
                session_metadata[session_id] = meta
            turns.extend(entry["state"].get("turns", []))
            file_stats = restore_parse_stats(entry["stats"])
            for key, value in asdict(file_stats).items():
                if key not in ("runtime", "scan", "files_seen"):
                    setattr(stats, key, getattr(stats, key) + value)
            for key, value in asdict(file_stats.runtime).items():
                setattr(stats.runtime, key, getattr(stats.runtime, key) + value)
            for session, parent in entry["state"].get("parents", {}).items():
                if session not in parent_threads or parent:
                    parent_threads[session] = parent
            tracker = RunningIntervalTracker(log, stats.runtime)
            restore_cached_tracker(tracker, entry["state"])
            tracker.finish_open_turn()
            running_intervals.extend(tracker.intervals)

        fingerprints: set[tuple[object, ...]] = set()
        records = []
        for record, fingerprint in cache.load_events(active):
            if fingerprint in fingerprints:
                stats.duplicate_events_removed += 1
            else:
                fingerprints.add(fingerprint)
                records.append(record)
        frame = frame_from_records(records, parent_threads, titles, target_tz)
        frame = enrich_thread_metadata(frame, codex_home, session_metadata)
        running_intervals = deduplicate_running_intervals(running_intervals, stats.runtime)
        populate_usage_inventory(inventory, session_metadata, parent_threads, turns)
    scan.seconds = perf_counter() - started
    if progress:
        eprint(f"{scan.mode.capitalize()} scan: {scan.bytes_read / 1024**2:.2f} MiB parsed "
               f"in {scan.seconds:.2f}s; cache: {cache_path}")
    return frame, running_intervals, stats


def collect_usage_data(
    codex_home: Path,
    target_tz: tzinfo,
    include_archived: bool = True,
    progress: bool = True,
    *,
    cache_path: Path | None = None,
    incremental: bool = False,
    inventory: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, list[RunningInterval], ParseStats]:
    if cache_path is not None:
        cache_path = cache_path.expanduser().resolve()
        try:
            return collect_cached_usage_data(
                codex_home, target_tz, include_archived, progress, cache_path, incremental,
                inventory=inventory,
            )
        except (CacheInvalidError, sqlite3.DatabaseError) as exc:
            if isinstance(exc, sqlite3.DatabaseError):
                code = getattr(exc, "sqlite_errorcode", None)
                corrupt = code in (getattr(sqlite3, "SQLITE_CORRUPT", 11),
                                   getattr(sqlite3, "SQLITE_NOTADB", 26))
                if not corrupt and not any(text in str(exc).lower() for text in (
                    "database disk image is malformed", "file is not a database",
                )):
                    raise
            backup = cache_path.with_name(
                f"{cache_path.name}.invalid-{datetime.now():%Y%m%d%H%M%S%f}.bak"
            )
            cache_path.replace(backup)
            if progress:
                eprint(f"缓存不可用，已保留为 {backup.name}；正在重新完整读取。")
            return collect_cached_usage_data(
                codex_home, target_tz, include_archived, progress, cache_path, incremental,
                inventory=inventory,
            )
    report_progress("discover", "正在查找日志…", enabled=progress)
    files = discover_rollouts(codex_home, include_archived)
    stats = ParseStats(files_seen=len(files))
    reporter = ProgressReporter("scan", "正在检查日志", total=len(files), unit="个文件", enabled=progress)
    reporter.update(0)
    titles = load_session_titles(codex_home)
    records: list[dict[str, Any]] = []
    fingerprints: set[tuple[object, ...]] = set()
    running_intervals: list[RunningInterval] = []
    parent_threads: dict[str, str] = {}
    session_metadata: dict[str, dict[str, Any]] = {}
    turns: list[dict[str, Any]] = []

    for index, path in enumerate(files, start=1):
        tracker = RunningIntervalTracker(relative_log_path(path, codex_home), stats.runtime)
        cursor: dict[str, Any] = {}
        for record, fingerprint in parse_rollout(
            path, codex_home, target_tz, titles, stats, tracker, parent_threads, cursor
        ):
            if fingerprint in fingerprints:
                stats.duplicate_events_removed += 1
                continue
            fingerprints.add(fingerprint)
            records.append(record)
        meta = cursor.get("meta", {})
        session_id = first_nonempty(meta.get("id"), meta.get("session_id"))
        if session_id:
            session_metadata[session_id] = meta
        turns.extend(cursor.get("turns", []))
        tracker.finish_open_turn()
        running_intervals.extend(tracker.intervals)
        reporter.update(index, f"保留 {len(records):,} 条用量事件，读取失败 {stats.files_failed} 个文件")

    report_progress("organize", "正在整理用量与运行时间…", enabled=progress)
    frame = frame_from_records(records, parent_threads, titles, target_tz)
    frame = enrich_thread_metadata(frame, codex_home, session_metadata)
    populate_usage_inventory(inventory, session_metadata, parent_threads, turns)
    return frame, deduplicate_running_intervals(running_intervals, stats.runtime), stats


def collect_usage_events(
    codex_home: Path,
    target_tz: tzinfo,
    include_archived: bool = True,
    progress: bool = True,
) -> tuple[pd.DataFrame, ParseStats]:
    """Keep the token-only return value available to existing callers."""
    events, _intervals, stats = collect_usage_data(
        codex_home, target_tz, include_archived, progress
    )
    return events, stats


def parse_date_argument(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} 请使用 YYYY-MM-DD 日期格式：{value}") from exc


def filter_date_range(
    events: pd.DataFrame, start: date | None, end: date | None
) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    local_dates = events["timestamp"].dt.date
    mask = pd.Series(True, index=events.index)
    if start is not None:
        mask &= local_dates >= start
    if end is not None:
        mask &= local_dates <= end
    return events.loc[mask].reset_index(drop=True)


def aggregate_daily(
    events: pd.DataFrame, dates: pd.DatetimeIndex | None = None
) -> pd.DataFrame:
    indexed = events.set_index("timestamp")
    numeric_columns = [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]
    daily = indexed[numeric_columns].resample("D").sum()
    daily["requests"] = indexed["total_tokens"].resample("D").count()
    # Empty string-typed groups may keep their dtype in pandas 3; this is a count.
    daily["threads"] = indexed["thread_id"].resample("D").nunique().astype("int64")
    if dates is not None:
        daily = daily.reindex(dates, fill_value=0)
        daily.index.name = "timestamp"
    daily["rolling_7d_tokens"] = daily["total_tokens"].rolling(7, min_periods=1).mean()
    daily["cumulative_tokens"] = daily["total_tokens"].cumsum()
    daily["cache_miss_percent"] = (
        100 - daily["cached_input_tokens"] / daily["input_tokens"].where(daily["input_tokens"] > 0) * 100
    )
    rolling_input = daily["input_tokens"].rolling(7, min_periods=1).sum()
    rolling_cached = daily["cached_input_tokens"].rolling(7, min_periods=1).sum()
    daily["rolling_7d_cache_miss_percent"] = (
        100 - rolling_cached / rolling_input.where(rolling_input > 0) * 100
    )
    return daily.reset_index().rename(columns={"timestamp": "date"})


def aggregate_recent_hourly(
    events: pd.DataFrame, target_tz: tzinfo, end: date
) -> pd.DataFrame:
    """Sum requests in each actual hour of the seven local dates ending at end."""
    lower = local_midnight(end - timedelta(days=6), target_tz)
    upper = local_midnight(end + timedelta(days=1), target_tz)
    hours = pd.date_range(lower, upper, freq="h", inclusive="left")
    local_events = events.assign(timestamp=events["timestamp"].dt.tz_convert(target_tz))
    selected = local_events.loc[
        (local_events["timestamp"] >= lower) & (local_events["timestamp"] < upper)
    ].set_index("timestamp")
    numeric_columns = [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]
    hourly = selected[numeric_columns].resample("h", origin=lower).sum()
    hourly["requests"] = selected["total_tokens"].resample("h", origin=lower).count()
    hourly["threads"] = selected["thread_id"].resample("h", origin=lower).nunique()
    # pandas 3 may preserve a string dtype for an empty thread-count result.
    # Establish numeric counts before filling missing calendar hours with zero.
    hourly = hourly.astype("int64").reindex(hours, fill_value=0)
    hourly.index.name = "hour_start"
    return hourly.reset_index()


def add_week_start(events: pd.DataFrame) -> pd.DataFrame:
    result = events.copy()
    local_midnight = result["timestamp"].dt.normalize()
    result["week_start"] = local_midnight - pd.to_timedelta(
        result["timestamp"].dt.day_of_week, unit="D"
    )
    return result


def aggregate_weekly(events: pd.DataFrame) -> pd.DataFrame:
    with_week = add_week_start(events)
    numeric_columns = [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]
    weekly = with_week.groupby("week_start", as_index=False)[numeric_columns].sum()
    requests = with_week.groupby("week_start").size().rename("requests")
    threads = with_week.groupby("week_start")["thread_id"].nunique().rename("threads")
    weekly = weekly.merge(requests, on="week_start").merge(threads, on="week_start")
    weekly["cumulative_tokens"] = weekly["total_tokens"].cumsum()
    return weekly


def aggregate_threads(events: pd.DataFrame) -> pd.DataFrame:
    descendants = events["session_id"] != events["thread_id"]
    attributed = events.assign(
        main_agent_tokens=events["total_tokens"].where(~descendants, 0),
        subagent_tokens=events["total_tokens"].where(descendants, 0),
        subagent_session_id=events["session_id"].where(descendants, pd.NA),
        project_name=events["project_name"] if "project_name" in events else events["cwd"].map(directory_name),
        thread_started_at=events["thread_started_at"] if "thread_started_at" in events else events["timestamp"],
    )
    return (
        attributed.groupby(["thread_id", "title"], dropna=False)
        .agg(
            total_tokens=("total_tokens", "sum"),
            main_agent_tokens=("main_agent_tokens", "sum"),
            subagent_tokens=("subagent_tokens", "sum"),
            subagent_sessions=("subagent_session_id", "nunique"),
            requests=("total_tokens", "size"),
            first_seen=("timestamp", "min"),
            last_seen=("timestamp", "max"),
            primary_source=("source", lambda values: values.value_counts().index[0]),
            primary_model=("model", lambda values: values.value_counts().index[0]),
            cwd=("cwd", "first"),
            project_name=("project_name", "first"),
            thread_started_at=("thread_started_at", "min"),
        )
        .reset_index()
        .sort_values("total_tokens", ascending=False)
    )


def aggregate_models(events: pd.DataFrame) -> pd.DataFrame:
    return (
        events.groupby("model", dropna=False)
        .agg(
            total_tokens=("total_tokens", "sum"),
            requests=("total_tokens", "size"),
            first_seen=("timestamp", "min"),
            last_seen=("timestamp", "max"),
        )
        .reset_index()
        .sort_values("total_tokens", ascending=False)
    )


def configure_matplotlib() -> None:
    installed = {font.name for font in fontManager.ttflist}
    preferred = [
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    selected = next((name for name in preferred if name in installed), "DejaVu Sans")
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [selected, "DejaVu Sans"],
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.alpha": 0.22,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )


def format_tokens(value: float, _position: object = None) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def setup_date_axis(axis: plt.Axes, target_tz: tzinfo | None = None) -> None:
    locator = mdates.AutoDateLocator(minticks=4, maxticks=10, tz=target_tz)
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=target_tz))


def save_figure(
    figure: plt.Figure, output_dir: Path, filename: str, image_format: str, dpi: int
) -> Path:
    path = output_dir / f"{filename}.{image_format}"
    staged: Path | None = None
    try:
        sink = _figure_sink.get()
        if sink is not None:
            sink(figure)
            return path
        # Render before replacing an existing image, which may be open in a
        # viewer on Windows. Stage directly in the output directory so the
        # replacement inherits its readable ACL, not a private temp-folder ACL.
        with tempfile.NamedTemporaryFile(
            prefix=".chart-", suffix=f".{image_format}", dir=output_dir, delete=False
        ) as handle:
            staged = Path(handle.name)
        figure.savefig(staged, dpi=dpi, bbox_inches="tight")
        staged.replace(path)
    finally:
        plt.close(figure)
        if staged is not None:
            staged.unlink(missing_ok=True)
    return path


def plot_daily_usage(
    daily: pd.DataFrame,
    events: pd.DataFrame,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, model_colors: dict[str, Any] | None = None,
) -> Path:
    model_daily = model_tokens_by_time(events, pd.DatetimeIndex(daily["date"]))
    figure, axis = plt.subplots(figsize=(13, 5.5))
    bottom = np.zeros(len(daily), dtype=float)
    for model, color in (model_colors if model_colors is not None else model_stack_colors(events)).items():
        if model not in model_daily:
            continue
        values = model_daily[model].to_numpy(dtype=float)
        axis.bar(
            daily["date"], values, width=0.82, bottom=bottom,
            color=color, linewidth=0, label=model,
        )
        bottom += values
    axis.plot(
        daily["date"],
        daily["rolling_7d_tokens"],
        color="#0F172A",
        linewidth=2.2,
        label="7-day rolling average",
    )
    axis.set(title="Daily Codex token usage by model", ylabel="Tokens")
    axis.set_ylim(bottom=0)
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    setup_date_axis(axis, daily["date"].dt.tz)
    axis.legend(frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.14), fontsize=9)
    if not bottom.any():
        axis.set_ylim(0, 1)
        axis.set_yticks([0, 1])
        axis.text(
            0.5, 0.5, "No token data", transform=axis.transAxes,
            ha="center", va="center", color="#64748B",
        )
    return save_figure(figure, output_dir, "01_daily_tokens", image_format, dpi)


def plot_recent_hourly_usage(
    hourly: pd.DataFrame,
    events: pd.DataFrame,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, model_colors: dict[str, Any] | None = None,
) -> Path:
    figure, axis = plt.subplots(figsize=(14, 5.5))
    hours = hourly["hour_start"]
    total = int(hourly["total_tokens"].sum())
    model_hourly = model_tokens_by_time(events, pd.DatetimeIndex(hours), "h")
    bottom = np.zeros(len(hourly), dtype=float)
    for model, color in (model_colors if model_colors is not None else model_stack_colors(events)).items():
        if model not in model_hourly:
            continue
        values = model_hourly[model].to_numpy(dtype=float)
        if not values.any():
            continue
        axis.bar(
            hours, values, width=0.86 / 24, align="edge", bottom=bottom,
            color=color, linewidth=0, label=model,
        )
        bottom += values
    setup_recent_hourly_axis(
        axis, hours, "Hourly Codex token usage by model — last 7 calendar days",
        "Tokens", format_tokens(total),
    )
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    if axis.containers:
        axis.legend(frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.22), fontsize=9)
    if total == 0:
        axis.set_ylim(0, 1)
        axis.set_yticks([0, 1])
        axis.text(
            0.5, 0.5, "No token usage in this 7-day window", transform=axis.transAxes,
            ha="center", va="center", color="#64748B",
        )
    return save_figure(figure, output_dir, "12_recent_hourly_tokens", image_format, dpi)


def setup_recent_hourly_axis(
    axis: plt.Axes, hours: pd.Series, title: str, ylabel: str, total_label: str
) -> None:
    """Share the local calendar and hour labels between the token and cost images."""
    target_tz = hours.dt.tz
    upper = hours.iloc[-1] + pd.Timedelta(hours=1)
    axis.set(
        title=(
            f"{title}\n"
            f"{hours.iloc[0]:%Y-%m-%d} to {hours.iloc[-1]:%Y-%m-%d}"
            f" | {target_tz} | total {total_label}"
        ),
        xlabel="Local date and hour", ylabel=ylabel,
        xlim=(hours.iloc[0], upper),
    )
    axis.set_ylim(bottom=0)
    setup_date_axis(axis, target_tz)
    axis.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=target_tz))
    axis.xaxis.set_minor_formatter(mdates.DateFormatter("%H:%M", tz=target_tz))
    axis.tick_params(axis="x", which="major", pad=19)
    axis.tick_params(axis="x", which="minor", labelsize=8)


def model_tokens_by_time(
    events: pd.DataFrame, periods: pd.DatetimeIndex, frequency: str = "D"
) -> pd.DataFrame:
    """Sum every model on a complete local calendar, preserving actual hours."""
    local_events = events.assign(timestamp=events["timestamp"].dt.tz_convert(periods.tz))
    origin = periods[0] if len(periods) else "start_day"
    model_usage = local_events.groupby(
        [pd.Grouper(key="timestamp", freq=frequency, origin=origin), "model"]
    )["total_tokens"].sum().unstack("model", fill_value=0)
    return model_usage.reindex(periods, fill_value=0)


def cumulative_model_tokens(
    events: pd.DataFrame, dates: pd.DatetimeIndex
) -> pd.DataFrame:
    """Accumulate every model on the report's complete local calendar."""
    return model_tokens_by_time(events, dates).cumsum()


def model_stack_colors(events: pd.DataFrame) -> dict[str, Any]:
    """Use chart 08's colors and chart 02's first-seen stacking order."""
    colors = breakdown_color_map(weekly_pivot(events, "model").columns)
    model_order = events.groupby("model")["timestamp"].min().sort_values(kind="stable").index.tolist()
    if AUTO_REVIEW_MODEL in model_order:
        model_order.remove(AUTO_REVIEW_MODEL)
        model_order.append(AUTO_REVIEW_MODEL)
    return {model: colors[model] for model in model_order}


def plot_cumulative_usage(
    daily: pd.DataFrame,
    events: pd.DataFrame,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, model_colors: dict[str, Any] | None = None,
) -> Path:
    cumulative = cumulative_model_tokens(events, pd.DatetimeIndex(daily["date"]))
    figure, axis = plt.subplots(figsize=(13, 5.8))
    dates = daily["date"].dt.tz_localize(None)
    # Give a one-day report a visible filled area rather than a zero-width polygon.
    if len(dates) == 1:
        dates = pd.DatetimeIndex([dates.iloc[0], dates.iloc[0] + pd.Timedelta(days=1)])
    bottom = np.zeros(len(dates), dtype=float)
    if not cumulative.empty:
        for model, color in (model_colors if model_colors is not None else model_stack_colors(events)).items():
            if model not in cumulative:
                continue
            values = cumulative[model].to_numpy(dtype=float)
            if len(values) == 1:
                values = np.repeat(values, 2)
            top = bottom + values
            axis.fill_between(
                dates, bottom, top, color=color, alpha=1.0,
                linewidth=0, edgecolor="none", label=f"{model}: {format_tokens(values[-1])}",
            )
            bottom = top
    total = int(bottom[-1]) if len(bottom) else 0
    axis.set(
        title=f"Cumulative local token usage by model — total {format_tokens(total)}",
        ylabel="Tokens",
    )
    axis.set_ylim(bottom=0)
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    setup_date_axis(axis)
    if axis.collections:
        axis.legend(frameon=False, ncol=2, loc="upper left", fontsize=9)
        if total == 0:
            axis.set_ylim(0, 1)
            axis.set_yticks([0, 1])
    else:
        axis.text(
            0.5, 0.5, "No token data", transform=axis.transAxes,
            ha="center", va="center", color="#64748B",
        )
        axis.xaxis.set_major_formatter(NullFormatter())
        axis.set_xticks([])
        axis.set_yticks([])
    return save_figure(figure, output_dir, "02_cumulative_tokens", image_format, dpi)


def plot_daily_composition(
    daily: pd.DataFrame, output_dir: Path, image_format: str, dpi: int,
    *, component_colors: dict[str, Any] | None = None,
) -> Path:
    active_columns = [column for column in COMPOSITION_COLUMNS if daily[column].sum() > 0]
    figure, axis = plt.subplots(figsize=(13, 6))
    for index, column in enumerate(active_columns):
        # Log axes cannot represent zero.  Treat zero-usage dates as gaps so
        # they are not mistaken for a positive floor value.
        values = daily[column].where(daily[column] > 0, np.nan)
        axis.plot(
            daily["date"],
            values,
            label=COMPOSITION_LABELS[column],
            color=component_colors[column] if component_colors is not None else PALETTE[index % len(PALETTE)],
            linewidth=2.0,
            marker="o",
            markersize=2.8,
            alpha=0.82,
        )
    axis.set_yscale("log")
    axis.set(title="Daily token composition (log scale)", ylabel="Tokens")
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    setup_date_axis(axis)
    if active_columns:
        axis.legend(frameon=False, ncol=min(3, len(active_columns)), loc="upper left")
    else:
        axis.set_ylim(1, 10)
        axis.text(0.5, 0.5, "No token data", transform=axis.transAxes, ha="center", va="center", color="#64748B")
    axis.grid(which="major", alpha=0.25)
    axis.grid(which="minor", alpha=0.08)
    return save_figure(figure, output_dir, "03_daily_composition", image_format, dpi)


def collapse_breakdown(pivot: pd.DataFrame, max_categories: int = 6) -> pd.DataFrame:
    ordered = pivot.sum().sort_values(ascending=False)
    keep = list(ordered.head(max_categories).index)
    result = pivot[keep].copy()
    remaining = [column for column in pivot.columns if column not in keep]
    if remaining:
        result["other"] = pivot[remaining].sum(axis=1)
    return result


def weekly_pivot(events: pd.DataFrame, category: str) -> pd.DataFrame:
    with_week = add_week_start(events)
    pivot = with_week.pivot_table(
        index="week_start",
        columns=category,
        values="total_tokens",
        aggfunc="sum",
        fill_value=0,
    ).sort_index()
    if category == "model":
        # New models can dominate recent weeks while ranking below older
        # models over the full history. Keep every recorded model name.
        ordered = pivot.sum().sort_values(ascending=False).index
        return pivot.loc[:, ordered]
    return collapse_breakdown(pivot)


def breakdown_color_map(labels: Iterable[str]) -> dict[str, Any]:
    """Keep category colors consistent when another chart omits some labels."""
    labels = [str(label) for label in labels]
    colors = PALETTE if len(labels) <= len(PALETTE) else plt.get_cmap("tab20").colors
    return {label: colors[index % len(colors)] for index, label in enumerate(labels)}


def plot_weekly_breakdown(
    events: pd.DataFrame,
    category: str,
    title: str,
    filename: str,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, category_colors: dict[str, Any] | None = None,
) -> Path:
    pivot = weekly_pivot(events, category)
    figure, axis = plt.subplots(figsize=(13, 5.8))
    colors = category_colors if category_colors is not None else breakdown_color_map(pivot.columns)
    if category_colors is not None:
        pivot = pivot.reindex(columns=list(colors), fill_value=0)
    x_values = mdates.date2num(pivot.index.to_pydatetime())
    bottom = np.zeros(len(pivot), dtype=float)
    for column in pivot.columns:
        values = pivot[column].to_numpy(dtype=float)
        axis.bar(
            x_values,
            values,
            width=5.5,
            bottom=bottom,
            color=colors[str(column)],
            label=str(column),
            alpha=0.82,
        )
        bottom += values
    axis.xaxis_date()
    setup_date_axis(axis)
    axis.set(title=title, ylabel="Tokens")
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    if pivot.empty:
        axis.xaxis.set_major_formatter(NullFormatter())
        axis.set_xticks([])
        axis.set_yticks([])
        axis.text(0.5, 0.5, "No token data", transform=axis.transAxes, ha="center", va="center")
    elif len(pivot.columns) > 6:
        axis.legend(
            frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.12)
        )
    else:
        axis.legend(frameon=False, ncol=min(4, len(pivot.columns)), loc="upper left")
    return save_figure(figure, output_dir, filename, image_format, dpi)


def plot_activity_heatmap(
    events: pd.DataFrame, output_dir: Path, image_format: str, dpi: int
) -> Path:
    heat_source = events.assign(
        weekday=events["timestamp"].dt.day_of_week,
        hour=events["timestamp"].dt.hour,
    )
    heat = (
        heat_source.pivot_table(
            index="weekday",
            columns="hour",
            values="total_tokens",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(index=range(7), columns=range(24), fill_value=0)
        .astype(float)
    )
    values = heat.to_numpy()
    positive = values[values > 0]
    figure, axis = plt.subplots(figsize=(13, 4.8))
    vmin = max(float(positive.min()), 1.0) if positive.size else 1.0
    vmax = max(float(positive.max()), vmin * 1.01) if positive.size else vmin * 1.01
    masked = np.ma.masked_less_equal(values, 0)
    # Log scales mask zero; paint those cells with the lowest color instead of white.
    cmap = plt.get_cmap("rainbow")
    cmap = cmap.with_extremes(bad=cmap(0.0))
    image = axis.imshow(
        masked,
        aspect="auto",
        interpolation="nearest",
        cmap=cmap,
        norm=LogNorm(vmin=vmin, vmax=vmax),
    )
    if positive.size:
        colorbar = figure.colorbar(image, ax=axis, pad=0.015)
        colorbar.set_label("Tokens (log scale)")
        colorbar.ax.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    axis.set_xticks(range(24))
    axis.set_xticklabels([str(hour) for hour in range(24)])
    axis.set_yticks(range(7))
    axis.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    axis.set(title="Token activity by local weekday and hour", xlabel="Hour")
    axis.grid(False)
    return save_figure(figure, output_dir, "05_weekday_hour_heatmap", image_format, dpi)


def truncate_label(value: object, length: int = 48) -> str:
    text = str(value)
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


def plot_top_threads(
    threads: pd.DataFrame,
    events: pd.DataFrame,
    top_n: int,
    output_dir: Path,
    image_format: str,
    dpi: int,
    *, model_colors: dict[str, Any] | None = None,
) -> Path:
    # Imported locally because the cost calculator shares the chart helpers.
    from .cost import AUTO_REVIEW_MODEL, aggregate_thread_model_costs

    selected = threads.head(top_n).sort_values("total_tokens", ascending=True).copy()
    labels = [
        f"{truncate_label(row.project_name, 40)} · {row.thread_started_at:%Y-%m-%d}\n{truncate_label(row.title)}"
        for row in selected.itertuples()
    ]
    figure_height = max(5.5, 0.58 * len(selected) + 2.0)
    figure, axis = plt.subplots(figsize=(13.5, figure_height))
    model_threads = events.groupby(["thread_id", "model"])["total_tokens"].sum().unstack("model", fill_value=0)
    model_threads = model_threads.reindex(selected["thread_id"], fill_value=0)
    costs = aggregate_thread_model_costs(events.loc[events["thread_id"].isin(selected["thread_id"])])
    model_costs = costs.set_index(["thread_id", "model"])
    thread_costs = costs.groupby("thread_id")[["total_cost_usd", "priced_tokens"]].sum(min_count=1)

    def cost_label(value: float) -> str:
        if pd.isna(value):
            return "N/A"
        amount = f"${value:,.2f}" if value == 0 or value >= 0.005 else "<$0.01"
        return amount

    cost_labels = []
    left = np.zeros(len(selected), dtype=float)
    for model, color in (model_colors if model_colors is not None else model_stack_colors(events)).items():
        if model not in model_threads:
            continue
        values = model_threads[model].to_numpy(dtype=float)
        if not values.any():
            continue
        bars = axis.barh(np.arange(len(selected)), values, left=left, color=color, linewidth=0, label=model)
        red, green, blue = to_rgb(color)
        text_color = "#172033" if 0.299 * red + 0.587 * green + 0.114 * blue > 0.58 else "white"
        for position, (thread_id, bar) in enumerate(zip(selected["thread_id"], bars)):
            if bar.get_width() <= 0:
                continue
            if model == AUTO_REVIEW_MODEL or (thread_id, model) not in model_costs.index:
                continue
            estimate = model_costs.loc[(thread_id, model)]
            label = cost_label(estimate["total_cost_usd"])
            text = axis.text(
                bar.get_x() + bar.get_width() / 2, position, label,
                ha="center", va="center", fontsize=9, color=text_color,
            )
            cost_labels.append((text, bar))
        left += values
    axis.set(
        title=(
            f"Top {len(selected)} main threads by model token usage (including subagents)"
            if not selected.empty else "Main thread token usage by model (including subagents)"
        ),
        xlabel="Tokens",
    )
    axis.xaxis.set_major_formatter(FuncFormatter(format_tokens))
    axis.set_yticks(np.arange(len(selected)), labels=labels)
    axis.grid(axis="y", visible=False)
    axis.set_xlim(0, max(float(selected["total_tokens"].max()) * 1.14, 1) if not selected.empty else 1)
    if axis.containers:
        axis.legend(frameon=False, ncol=2, loc="lower right", fontsize=9)
    if selected.empty:
        axis.text(0.5, 0.5, "No token data", transform=axis.transAxes, ha="center", va="center")
        axis.set_xticks([])
        axis.set_yticks([])
    token_labels = []
    for position, value in enumerate(selected["total_tokens"]):
        text = axis.text(
            float(value),
            position,
            f"  {format_tokens(float(value))}",
            va="center",
            fontsize=9,
        )
        token_labels.append(text)
    if not selected.empty:
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        for position, thread_id in enumerate(selected["thread_id"]):
            total_label = average_label = "—"
            if thread_id in thread_costs.index:
                estimate = thread_costs.loc[thread_id]
                total_label = cost_label(estimate["total_cost_usd"])
                average_label = "N/A"
                if estimate["priced_tokens"] > 0:
                    average_label = cost_label(
                        estimate["total_cost_usd"] / estimate["priced_tokens"] * 1_000_000,
                    )
            offset = token_labels[position].get_window_extent(renderer).width * 72 / figure.dpi + 5
            axis.annotate(
                f"{total_label}\n{average_label} / 1M",
                xy=(float(selected.iloc[position]["total_tokens"]), position),
                xytext=(offset, 0), textcoords="offset points", ha="left", va="center",
                fontsize=9, linespacing=1.5, color="#172033",
            )
        # Omit prices that cannot fit inside their model's token segment.
        for text, bar in cost_labels:
            if text.get_window_extent(renderer).width + 8 > bar.get_window_extent(renderer).width:
                text.remove()
    return save_figure(figure, output_dir, "06_top_threads", image_format, dpi)


def gaussian_point_density(counts: np.ndarray, sigma_pixels: float) -> np.ndarray:
    """Sum Gaussian point profiles on a raster without an extra dependency."""
    # Keep faint tails even when thousands of requests occupy the same pixel.
    radius = max(1, math.ceil(6 * sigma_pixels))
    offsets = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-0.5 * (offsets / sigma_pixels) ** 2)
    density = counts.astype(np.float32)
    # A kernel peak of one gives an isolated point the same opacity at any DPI.
    for dimension in (0, 1):
        padding = [(0, 0), (0, 0)]
        padding[dimension] = (radius, radius)
        padded = np.pad(density, padding)
        blurred = np.zeros_like(density)
        window = [slice(None), slice(None)]
        for offset, weight in enumerate(kernel):
            window[dimension] = slice(offset, offset + density.shape[dimension])
            blurred += padded[tuple(window)] * weight
        density = blurred
    return density


def plot_request_sizes_over_time(
    events: pd.DataFrame, output_dir: Path, image_format: str, dpi: int,
    *, source_colors: dict[str, Any] | None = None,
) -> Path:
    limit = 60_000
    sampled = (
        events.sample(limit, random_state=42).sort_values("timestamp")
        if len(events) > limit
        else events
    )
    plot_data = sampled.loc[
        sampled["timestamp"].notna() & (sampled["total_tokens"] > 0)
    ].copy()
    figure, axis = plt.subplots(figsize=(13, 5.5))
    axis.set_yscale("log")
    axis.set(title="Per-request token size over time", ylabel="Tokens per request")
    axis.yaxis.set_major_formatter(FuncFormatter(format_tokens))
    setup_date_axis(axis)
    if plot_data.empty:
        axis.text(
            0.5, 0.5, "No positive token data", transform=axis.transAxes,
            ha="center", va="center", color="#64748B",
        )
        axis.xaxis.set_major_formatter(NullFormatter())
        axis.set_xticks([])
        axis.set_yticks([])
        axis.minorticks_off()
    else:
        plot_data["plot_date"] = mdates.date2num(plot_data["timestamp"])
        plot_data["plot_log_tokens"] = np.log10(plot_data["total_tokens"])
        dates = plot_data["plot_date"]
        log_tokens = plot_data["plot_log_tokens"]
        date_padding = max(float(dates.max() - dates.min()) * 0.05, 0.5)
        log_padding = max(float(log_tokens.max() - log_tokens.min()) * 0.05, 0.12)
        date_bounds = (float(dates.min()) - date_padding, float(dates.max()) + date_padding)
        log_bounds = (float(log_tokens.min()) - log_padding, float(log_tokens.max()) + log_padding)
        axis.set_xlim(*date_bounds)
        axis.set_ylim(10 ** log_bounds[0], 10 ** log_bounds[1])

        # Blur in display space, including log(Y), so points stay round. Bound
        # raster memory for high-DPI exports; axes and text retain their detail.
        raster_dpi = min(dpi, 200)
        position = axis.get_position()
        width = max(32, math.ceil(figure.get_figwidth() * position.width * raster_dpi))
        height = max(32, math.ceil(figure.get_figheight() * position.height * raster_dpi))
        shape = (height, width)
        total_density = np.zeros(shape, dtype=np.float32)
        weighted_rgb = np.zeros((*shape, 3), dtype=np.float32)
        for index, (source, group) in enumerate(plot_data.groupby("source")):
            color = source_colors[source] if source_colors is not None else PALETTE[index % len(PALETTE)]
            counts, _, _ = np.histogram2d(
                group["plot_log_tokens"], group["plot_date"],
                bins=shape, range=(log_bounds, date_bounds),
            )
            density = gaussian_point_density(counts, sigma_pixels=0.75 * raster_dpi / 72)
            total_density += density
            weighted_rgb += density[:, :, None] * np.asarray(to_rgb(color), dtype=np.float32)
            axis.scatter([], [], s=11, alpha=0.75, color=color, label=str(source), linewidths=0)

        # Mix all sources together before drawing to avoid covering earlier
        # sources. Compress opacity so dense clusters keep tonal differences.
        rgba = np.zeros((*shape, 4), dtype=np.float32)
        np.divide(
            weighted_rgb, total_density[:, :, None], out=rgba[:, :, :3],
            where=total_density[:, :, None] > 0,
        )
        strength = np.log1p(total_density)
        rgba[:, :, 3] = 0.88 * strength / (strength + 1.05)
        axis.imshow(
            rgba, origin="lower", aspect="auto", interpolation="bilinear",
            extent=(*date_bounds, 0, 1), transform=axis.get_xaxis_transform(), zorder=2,
        )
        axis.legend(frameon=False, ncol=4, loc="upper left")
    return save_figure(figure, output_dir, "07_request_sizes_over_time", image_format, dpi)


def running_time_line_coordinates(
    dates: pd.Series, hours: pd.Series,
) -> tuple[np.ndarray, np.ndarray]:
    """Keep linear interpolation accurate on both sides of the 24-hour knee."""
    x = mdates.date2num(dates.to_numpy())
    y = hours.to_numpy(dtype=float)
    line_x: list[float] = []
    line_y: list[float] = []
    for index, (day, value) in enumerate(zip(x, y)):
        if index:
            previous = y[index - 1]
            if previous < 24 < value or value < 24 < previous:
                fraction = (24 - previous) / (value - previous)
                line_x.append(x[index - 1] + fraction * (day - x[index - 1]))
                line_y.append(24.0)
        line_x.append(day)
        line_y.append(value)
    return np.asarray(line_x), np.asarray(line_y)


def plot_daily_running_time(
    daily: pd.DataFrame,
    output_dir: Path,
    target_tz: tzinfo,
    image_format: str,
    dpi: int,
) -> Path:
    figure, axis = plt.subplots(figsize=(13, 6.5))
    figure.subplots_adjust(left=0.085, right=0.915, bottom=0.12, top=0.855)
    summed_axis = axis.twinx()
    merged_color = PALETTE[0]
    summed_color = "#D97706"
    # These are calendar-date labels, so avoid shifting them to UTC on the axis.
    dates = daily["date"].dt.tz_localize(None)
    bars = axis.bar(
        dates,
        daily["running_hours"],
        width=0.82,
        color=merged_color,
        alpha=0.65,
        label="Merged running time (left)",
    )
    for bar, complete in zip(bars, daily["is_complete_day"]):
        if not complete:
            bar.set_hatch("///")
            bar.set_edgecolor("#1E3A8A")
            bar.set_alpha(0.4)
    average_line, = axis.plot(
        *running_time_line_coordinates(dates, daily["rolling_7d_running_hours"]),
        color="#1E40AF",
        linewidth=2.2,
        marker="o" if len(daily) == 1 else None,
        markersize=3,
        label="7-day merged average (left)",
    )
    summed_line, = summed_axis.plot(
        *running_time_line_coordinates(dates, daily["summed_running_hours"]),
        color=summed_color,
        linewidth=1.9,
        marker="o" if len(daily) == 1 else None,
        markersize=3,
        label="Summed session time (right)",
    )
    figure.suptitle(f"Daily Codex running time — {target_tz}", y=0.97, fontsize=14)
    axis.set_ylabel("Merged running time (hours)")
    axis.tick_params(axis="y", colors=merged_color)
    axis.yaxis.label.set_color(merged_color)
    summed_axis.set_ylabel("Summed session time (hours)")
    # Both axes use the same continuous, invertible hour mapping. Place 24 h
    # near the middle (52% from the bottom); the upper range stays linear.
    max_hours = max(
        24.0,
        float(daily["day_seconds"].max()) / 3600,
        float(daily["running_hours"].max()),
        float(daily["summed_running_hours"].max()),
    )
    hour_ticks = np.arange(0, 25, 4, dtype=float)
    upper_scale = 1.0
    if max_hours > 24:
        upper_ticks = MaxNLocator(
            nbins=3, integer=True, steps=[1, 2, 3, 4, 6, 8, 10],
        ).tick_values(24, max(48, max_hours))
        upper_ticks = upper_ticks[upper_ticks > 24]
        max_hours = max(upper_ticks[-1], 24 + (max_hours - 24) * 1.06)
        upper_scale = 24 * (0.48 / 0.52) / (max_hours - 24)
        hour_ticks = np.concatenate((hour_ticks, upper_ticks))

    def forward(values: np.ndarray) -> np.ndarray:
        values = np.asarray(values)
        return np.where(values <= 24, values, 24 + (values - 24) * upper_scale)

    def inverse(values: np.ndarray) -> np.ndarray:
        values = np.asarray(values)
        return np.where(values <= 24, values, 24 + (values - 24) / upper_scale)

    for hours_axis in (axis, summed_axis):
        hours_axis.set_yscale("function", functions=(forward, inverse))
        hours_axis.set_ylim(0, max_hours)
        hours_axis.set_yticks(hour_ticks)
        hours_axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    summed_axis.tick_params(axis="y", colors=summed_color)
    summed_axis.yaxis.label.set_color(summed_color)
    summed_axis.spines["right"].set_visible(True)
    summed_axis.spines["right"].set_color(summed_color)
    summed_axis.grid(False)
    setup_date_axis(axis)
    if not dates.empty:
        axis.set_xlim(dates.iloc[0] - pd.Timedelta(days=0.8), dates.iloc[-1] + pd.Timedelta(days=0.8))
        if len(dates) <= 10:
            axis.set_xticks(dates)
            if len(dates) == 1:
                axis.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %Y"))
    else:
        axis.set_xticks([])
        axis.xaxis.set_major_formatter(NullFormatter())
    if upper_scale < 1:
        axis.axhspan(24, max_hours, color="#F1F5F9", alpha=0.65, zorder=0)
        axis.axhline(24, color="#64748B", linewidth=1, linestyle=(0, (5, 4)))
        axis.text(
            0.012, 24, "24 h · scale changes here", transform=axis.get_yaxis_transform(),
            va="bottom", fontsize=8.5, color="#475569",
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 3},
        )
        scale_note = "Piecewise linear scale: 0–24 h expanded · above 24 h compressed"
    else:
        scale_note = "Linear hour scale · same hours at the same height on both axes"
    figure.text(0.085, 0.91, scale_note, fontsize=9, color="#475569")
    if not daily.empty and daily["summed_running_hours"].max() > 24:
        peak_index = int(np.argmax(daily["summed_running_hours"].to_numpy()))
        peak_date = dates.iloc[peak_index]
        peak_hours = float(daily["summed_running_hours"].iloc[peak_index])
        label_on_left = peak_index >= len(daily) / 2
        label_above = peak_hours < 24 + (max_hours - 24) * 0.35
        summed_axis.plot(peak_date, peak_hours, "o", color=summed_color, markersize=4)
        summed_axis.annotate(
            f"Peak: {peak_hours:.1f} h\n{peak_date:%d %b %Y}",
            xy=(peak_date, peak_hours),
            xytext=(-12 if label_on_left else 12, 12 if label_above else -14),
            textcoords="offset points", ha="right" if label_on_left else "left",
            va="bottom" if label_above else "top",
            fontsize=8.5, color=summed_color,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.9, "pad": 3},
        )
    if daily.empty or not daily["summed_running_hours"].gt(0).any():
        axis.text(
            0.5, 0.5, "No running activity in this date range", transform=axis.transAxes,
            ha="center", va="center", color="#64748B",
        )
    if not daily["is_complete_day"].all():
        figure.text(0.085, 0.025, "Hatched bar: partial local day", fontsize=8, color="#64748B")
    axis.legend(
        handles=[bars, average_line, summed_line], frameon=False, ncol=3,
        loc="lower left", bbox_to_anchor=(0, 1.02), borderaxespad=0, fontsize=9,
    )
    return save_figure(figure, output_dir, "10_daily_running_time", image_format, dpi)


def plot_daily_cache_miss_rate(
    daily: pd.DataFrame, output_dir: Path, image_format: str, dpi: int
) -> Path:
    figure, axis = plt.subplots(figsize=(13, 5.8))
    dates = daily["date"].dt.tz_localize(None)
    axis.plot(
        dates,
        daily["cache_miss_percent"],
        color=PALETTE[0],
        linewidth=1.2,
        marker="o",
        markersize=3.4,
        alpha=0.65,
        label="Daily cache miss rate",
    )
    axis.plot(
        dates,
        daily["rolling_7d_cache_miss_percent"],
        color="#0F766E",
        linewidth=2.2,
        label="7-day token-weighted rate",
    )
    total_input = int(daily["input_tokens"].sum())
    if total_input > 0:
        overall_percent = 100 - float(daily["cached_input_tokens"].sum()) / total_input * 100
        axis.axhline(
            overall_percent,
            color="#D97706",
            linewidth=1.5,
            linestyle="--",
            label=f"Overall: {overall_percent:.2f}%",
        )
        peak_percent = max(
            float(daily["cache_miss_percent"].max()),
            float(daily["rolling_7d_cache_miss_percent"].max()),
            overall_percent,
        )
        axis.set_ylim(0, min(100, max(1.0, peak_percent * 1.15)))
    else:
        axis.set_ylim(0, 100)
        axis.text(
            0.5, 0.5, "No input tokens in the selected date range",
            transform=axis.transAxes, ha="center", va="center", color="#64748B",
        )
    axis.set_title("Daily Codex cache miss rate", pad=42)
    axis.set_ylabel("Uncached input / total input")
    axis.yaxis.set_major_formatter(PercentFormatter(xmax=100))
    if not dates.empty:
        # All-NaN series do not establish date limits for Matplotlib.
        axis.set_xlim(dates.iloc[0] - pd.Timedelta(hours=12), dates.iloc[-1] + pd.Timedelta(hours=12))
    setup_date_axis(axis)
    axis.legend(frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.01), fontsize=9)
    figure.subplots_adjust(top=0.80)
    return save_figure(figure, output_dir, "11_daily_cache_miss_rate", image_format, dpi)


def dataframe_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if pd.api.types.is_datetime64_any_dtype(result[column]):
            result[column] = result[column].map(
                lambda value: value.isoformat() if pd.notna(value) else ""
            )
    return result


def export_report(
    events: pd.DataFrame,
    stats: ParseStats,
    output_dir: Path,
    target_tz: tzinfo,
    image_format: str,
    dpi: int,
    top_threads: int,
    *,
    running_intervals: list[RunningInterval],
    start: date,
    end: date,
    animation: AnimationOptions | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_progress("aggregate", "正在汇总图片数据…", enabled=progress)
    observed_at = datetime.now(timezone.utc)
    lower = local_midnight(start, target_tz).astimezone(timezone.utc)
    upper = local_midnight(end + timedelta(days=1), target_tz).astimezone(timezone.utc)
    selected_intervals = [
        item for item in running_intervals if item.end > lower and item.start < upper
    ]
    running_daily = aggregate_daily_running_time(
        selected_intervals, target_tz, start, end, observed_at
    )
    daily = aggregate_daily(events, pd.DatetimeIndex(running_daily["date"]))
    daily = daily.merge(running_daily, on="date", how="left")
    weekly = aggregate_weekly(events)
    threads = aggregate_threads(events)
    models = aggregate_models(events)
    recent_hourly = aggregate_recent_hourly(events, target_tz, end)
    # Import here because the cost module also reuses the chart styling helpers.
    from .cost import (
        OFFICIAL_PRICES, PRICE_DATE, aggregate_recent_hourly_cost,
        official_price_history, plot_recent_hourly_cost,
    )

    recent_hourly_cost = aggregate_recent_hourly_cost(events, pd.DatetimeIndex(recent_hourly["hour_start"]))

    report_progress("data", "正在保存用量数据文件…", enabled=progress)
    dataframe_for_csv(events).to_csv(output_dir / "events.csv", index=False, encoding="utf-8-sig")
    dataframe_for_csv(daily).to_csv(
        output_dir / "daily_usage.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(weekly).to_csv(
        output_dir / "weekly_usage.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(threads).to_csv(
        output_dir / "thread_usage.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(models).to_csv(
        output_dir / "model_usage.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(running_daily).to_csv(
        output_dir / "daily_running_time.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(running_intervals_frame(selected_intervals, target_tz)).to_csv(
        output_dir / "running_intervals.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(recent_hourly).to_csv(
        output_dir / "recent_hourly_usage.csv", index=False, encoding="utf-8-sig"
    )
    dataframe_for_csv(recent_hourly_cost).to_csv(
        output_dir / "recent_hourly_api_cost.csv", index=False, encoding="utf-8-sig"
    )

    configure_matplotlib()
    arguments = (output_dir, image_format, dpi)
    chart_jobs = [] if events.empty else [
        (1, plot_daily_usage, (daily, events, *arguments)),
        (2, plot_cumulative_usage, (daily, events, *arguments)),
        (3, plot_daily_composition, (daily, *arguments)),
        (4, plot_weekly_breakdown, (
            events,
            "source",
            "Weekly token usage by source",
            "04_weekly_by_source",
            *arguments,
        )),
        (5, plot_activity_heatmap, (events, *arguments)),
        (6, plot_top_threads, (threads, events, top_threads, *arguments)),
        (7, plot_request_sizes_over_time, (events, *arguments)),
        (8, plot_weekly_breakdown, (
            events,
            "model",
            "Weekly token usage by model",
            "08_weekly_by_model",
            *arguments,
        )),
    ]
    chart_jobs.extend([
        (10, plot_daily_running_time, (running_daily, output_dir, target_tz, image_format, dpi)),
        (11, plot_daily_cache_miss_rate, (daily, *arguments)),
        (12, plot_recent_hourly_usage, (recent_hourly, events, *arguments)),
        (13, plot_recent_hourly_cost, (recent_hourly_cost, events, *arguments)),
    ])
    reporter = ProgressReporter("charts", "正在生成用量图片", total=len(chart_jobs), unit="张", enabled=progress)
    chart_paths = []
    for index, (number, plot, plot_arguments) in enumerate(chart_jobs):
        reporter.update(index, f"正在生成图片 {number:02d}", force=True)
        chart_paths.append(plot(*plot_arguments))
        reporter.update(index + 1, f"已保存图片 {number:02d}", force=True)

    from .animations import AnimationOptions, export_animations

    animation_results = export_animations(
        events, selected_intervals, output_dir, target_tz, start,
        animation or AnimationOptions(), top_threads=top_threads, observed_at=observed_at,
        progress=progress,
    )

    total_input = int(events["input_tokens"].sum())
    total_cached = int(events["cached_input_tokens"].sum())
    summary: dict[str, Any] = {
        "scope": "Local Codex rollout logs only; not the server-side account usage total.",
        "timezone": str(target_tz),
        "coverage_start": (
            events["timestamp"].min().isoformat() if not events.empty
            else local_midnight(start, target_tz).isoformat()
        ),
        "coverage_end": (
            events["timestamp"].max().isoformat() if not events.empty
            else local_midnight(end, target_tz).isoformat()
        ),
        "active_days": int((daily["total_tokens"] > 0).sum()),
        "request_count": int(len(events)),
        "thread_count": int(events["thread_id"].nunique()),
        "model_count": int(events["model"].nunique()),
        "total_tokens": int(events["total_tokens"].sum()),
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "cache_miss_percent": 100 - total_cached / total_input * 100 if total_input > 0 else None,
        "output_tokens": int(events["output_tokens"].sum()),
        "reasoning_output_tokens": int(events["reasoning_output_tokens"].sum()),
        "peak_day": (
            daily.loc[daily["total_tokens"].idxmax(), "date"].isoformat()
            if not events.empty else None
        ),
        "peak_day_tokens": int(daily["total_tokens"].max()),
        "recent_hourly_api_cost": {
            "currency": "USD",
            "total_cost_usd": float(recent_hourly_cost["official_models_cost_usd"].sum()),
            "unpriced_requests": int(recent_hourly_cost["unpriced_requests"].sum()),
            "unpriced_tokens": int(recent_hourly_cost["unpriced_tokens"].sum()),
            "price_date": PRICE_DATE,
            "official_prices": {
                model: asdict(price) for model, price in OFFICIAL_PRICES.items()
                if f"model_{model}_cost_usd" in recent_hourly_cost.columns
            },
            "official_price_history": {
                model: periods for model, periods in official_price_history().items()
                if f"model_{model}_cost_usd" in recent_hourly_cost.columns
            },
            "assumptions": [
                "Sum per-request estimates using the recorded standard API token prices; not an actual bill.",
                "Models without recorded prices and tool-call charges are excluded.",
                "Rows with only total_tokens and no input/output breakdown contribute no estimated cost.",
            ],
        },
        "running_time": {
            "definition": "Daily union and sum of running turn intervals; percentages use the full local day.",
            "unfinished_turn_policy": "Count only through the last observed in-turn event.",
            "coverage_start_date": start.isoformat(),
            "coverage_end_date": end.isoformat(),
            "observed_at": observed_at.astimezone(target_tz).isoformat(),
            "day_count": len(running_daily),
            "running_days": int((running_daily["running_seconds"] > 0).sum()),
            "total_running_hours": float(running_daily["running_hours"].sum()),
            "total_summed_running_hours": float(running_daily["summed_running_hours"].sum()),
            "mean_daily_running_percent": float(running_daily["running_percent"].mean()),
            "peak_day": running_daily.loc[
                running_daily["running_percent"].idxmax(), "date"
            ].date().isoformat(),
            "peak_day_percent": float(running_daily["running_percent"].max()),
            "interval_count": len(selected_intervals),
            "incomplete_intervals": sum(item.status == "incomplete" for item in selected_intervals),
            "latest_day": running_daily.iloc[-1]["date"].date().isoformat(),
            "latest_day_running_hours": float(running_daily.iloc[-1]["running_hours"]),
            "latest_day_summed_running_hours": float(running_daily.iloc[-1]["summed_running_hours"]),
            "latest_day_running_percent": float(running_daily.iloc[-1]["running_percent"]),
            "latest_day_is_complete": bool(running_daily.iloc[-1]["is_complete_day"]),
        },
        "parse_stats": asdict(stats),
        "charts": [path.name for path in chart_paths],
        "animations": animation_results,
    }
    report_progress("summary", "正在保存用量汇总…", enabled=progress)
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_usage report",
        description="从本地 Codex 日志生成 token 用量、运行时间图片及数据文件。",
        epilog="首次使用示例：python -m codex_usage report --timezone Asia/Shanghai --incremental",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="包含 sessions 的数据目录（默认：CODEX_HOME 或 ~/.codex）。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("codex_usage_report"),
        help="结果目录（默认：当前工作目录下的 codex_usage_report）。",
    )
    parser.add_argument(
        "--timezone",
        default="local",
        help="IANA 时区，如 Asia/Shanghai；默认 local 使用系统当前 UTC 偏移。",
    )
    parser.add_argument("--start", help="起始本地日期，YYYY-MM-DD，包含当天。")
    parser.add_argument("--end", help="结束本地日期，YYYY-MM-DD，包含当天。")
    parser.add_argument(
        "--include-archived",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="包含 archived_sessions 中的日志（默认包含）。",
    )
    parser.add_argument(
        "--format", choices=("png", "svg", "pdf"), default="png", help="图片格式，默认 png。"
    )
    parser.add_argument("--dpi", type=int, default=160, help="图片 DPI，默认 160。")
    parser.add_argument(
        "--top-threads", type=int, default=15, help="任务排行数量，默认 15。"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="不显示扫描、图片及动画生成进度。"
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="复用统计缓存，仅读取新增或变化的日志；首次自动完整读取。",
    )
    parser.add_argument(
        "--cache-file", type=Path,
        default=Path(__file__).resolve().parents[1] / "codex_usage_cache.sqlite3",
        help="缓存文件位置，默认工具目录中的 codex_usage_cache.sqlite3。",
    )
    animation_group = parser.add_mutually_exclusive_group()
    animation_group.add_argument(
        "--animations", type=int, nargs="+", choices=range(1, 14), default=[2],
        metavar="NUMBER", help="生成指定编号的 MP4 动画，默认仅 02；例如 --animations 2 6。",
    )
    animation_group.add_argument("--no-animations", action="store_true", help="本次不生成动画。")
    parser.add_argument("--animation-start", help="动画第一帧的本地日期，默认结束日期的 30 天前。")
    parser.add_argument("--animation-end", help="动画最后一帧的本地日期，默认今天；指定 --end 时沿用该日期。")
    parser.add_argument("--animation-fps", type=int, default=4, help="动画每秒帧数，默认 4，每帧推进一个本地日。")
    parser.add_argument("--animation-dpi", type=int, default=100, help="动画 DPI，默认 100，与静态图片格式和 DPI 独立。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target_tz = resolve_timezone(args.timezone)
        start = parse_date_argument(args.start, "--start")
        end = parse_date_argument(args.end, "--end")
        from .animations import AnimationOptions, animation_dates

        animation = AnimationOptions(
            charts=() if args.no_animations else tuple(dict.fromkeys(args.animations)),
            start=parse_date_argument(args.animation_start, "--animation-start"),
            end=parse_date_argument(args.animation_end, "--animation-end") or end,
            fps=args.animation_fps, dpi=args.animation_dpi,
        )
        animation_dates(animation, target_tz)
    except ValueError as exc:
        eprint(f"错误：{exc}")
        return 2

    if start is not None and end is not None and start > end:
        eprint("错误：--start 不能晚于 --end。")
        return 2
    if args.dpi <= 0 or args.top_threads <= 0:
        eprint("错误：--dpi 和 --top-threads 必须大于 0。")
        return 2

    codex_home = args.codex_home.expanduser().resolve()
    if not codex_home.is_dir():
        eprint(f"错误：Codex 数据目录不存在：{codex_home}。请用 --codex-home 指定包含 sessions 的目录。")
        return 2

    events, running_intervals, stats = collect_usage_data(
        codex_home,
        target_tz,
        include_archived=args.include_archived,
        progress=not args.quiet,
        cache_path=args.cache_file.expanduser().resolve(),
        incremental=args.incremental,
    )
    coverage_dates = []
    if not events.empty:
        coverage_dates.extend([events["timestamp"].min().date(), events["timestamp"].max().date()])
    for interval in running_intervals:
        coverage_dates.append(interval.start.astimezone(target_tz).date())
        last_instant = interval.end - timedelta(microseconds=1) if interval.end > interval.start else interval.end
        coverage_dates.append(last_instant.astimezone(target_tz).date())
    if not coverage_dates:
        eprint(f"错误：在 {codex_home} 中未找到 token_count 或运行时间记录。请确认 sessions / archived_sessions 中保留了日志；尚无日志时可运行 python -m codex_usage demo 体验。")
        return 1
    report_start = start or min(coverage_dates)
    report_end = end or max(coverage_dates)
    if report_start > report_end:
        eprint("错误：所选日期范围没有活动，请调整 --start / --end。")
        return 1
    events = filter_date_range(events, report_start, report_end)

    output_dir = args.output.expanduser().resolve()
    try:
        summary = export_report(
            events,
            stats,
            output_dir,
            target_tz,
            args.format,
            args.dpi,
            args.top_threads,
            running_intervals=running_intervals,
            start=report_start,
            end=report_end,
            animation=animation,
            progress=not args.quiet,
        )
    except (OSError, RuntimeError) as exc:
        eprint(f"错误：图片或动画生成失败：{exc}")
        return 1
    print(f"Report: {output_dir}")
    print(
        f"Coverage: {summary['coverage_start']} to {summary['coverage_end']} "
        f"({summary['active_days']} active days)"
    )
    print(
        f"Local total: {summary['total_tokens']:,} tokens across "
        f"{summary['request_count']:,} requests and {summary['thread_count']:,} threads"
    )
    runtime = summary["running_time"]
    print(
        f"Running time: {runtime['total_running_hours']:.2f} merged hours, "
        f"{runtime['total_summed_running_hours']:.2f} summed session hours; "
        f"mean {runtime['mean_daily_running_percent']:.2f}% of each full day"
    )
    partial = " (partial day, full-day denominator)" if not runtime["latest_day_is_complete"] else ""
    print(
        f"Latest day {runtime['latest_day']}: {runtime['latest_day_running_hours']:.2f} merged hours, "
        f"{runtime['latest_day_summed_running_hours']:.2f} summed session hours, "
        f"{runtime['latest_day_running_percent']:.2f}%{partial}"
    )
    print(f"Generated {len(summary['charts'])} charts and 9 CSV tables")
    print(f"Generated {len(summary['animations'])} MP4 animations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
