"""Machine-readable local usage queries, sharing the report parser and deduplication."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone as datetime_timezone, tzinfo
from pathlib import Path
import subprocess
from typing import Any, Iterable

import pandas as pd

from . import __version__
from .cache import CACHE_VERSION
from .charts import (
    COMPOSITION_COLUMNS, RAW_TOKEN_COLUMNS, collect_usage_data, default_codex_home,
    resolve_thread_roots, resolve_timezone,
)
from .running_time import RunningInterval, merge_running_intervals


UTC = datetime_timezone.utc
SCHEMA_VERSION = 1
TOKEN_COLUMNS = [*RAW_TOKEN_COLUMNS, *COMPOSITION_COLUMNS]
EVENT_COLUMNS = [
    "timestamp", "session_id", "thread_id", "parent_thread_id", "turn_id",
    "model", "reasoning_effort", "source", *TOKEN_COLUMNS,
]


def _ids(values: Iterable[str], name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} 必须是字符串列表，不能传入单个字符串。")
    result = list(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError(f"{name} 中的每项必须是非空字符串。")
    return list(dict.fromkeys(result))


def _instant(value: datetime | str | None, target_tz: tzinfo, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是 ISO 8601 时间。") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{name} 必须是 datetime 或 ISO 8601 时间字符串。")
    if value.tzinfo is None:
        # Refuse to silently choose a side of a DST gap or repeated local time.
        candidates = [value.replace(tzinfo=target_tz, fold=fold) for fold in (0, 1)]
        valid = [item for item in candidates if item.astimezone(UTC).astimezone(target_tz).replace(tzinfo=None) == value]
        if not valid:
            raise ValueError(f"{name} 是夏令时切换中不存在的本地时间，请显式提供 UTC 偏移。")
        if len({item.utcoffset() for item in valid}) > 1:
            raise ValueError(f"{name} 是夏令时切换中有歧义的本地时间，请显式提供 UTC 偏移。")
        value = valid[0]
    return value.astimezone(UTC)


def _descendants(seeds: Iterable[str], parents: dict[str, str]) -> set[str]:
    selected = set(seeds)
    children: dict[str, set[str]] = defaultdict(set)
    for child, parent in parents.items():
        if parent:
            children[parent].add(child)
    pending = list(selected)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in selected:
                selected.add(child)
                pending.append(child)
    return selected


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    # pandas' JSON encoder rounds timestamps; keep the original ISO precision.
    return [
        {key: value.isoformat() if isinstance(value, (datetime, pd.Timestamp))
         else None if pd.isna(value) else value
         for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _totals(events: pd.DataFrame, intervals: list[RunningInterval]) -> dict[str, Any]:
    result = {column: int(events[column].sum()) for column in TOKEN_COLUMNS}
    result.update(
        usage_event_count=len(events),
        cache_miss_percent=(100 - result["cached_input_tokens"] / result["input_tokens"] * 100)
        if result["input_tokens"] else None,
        running_seconds=sum((end - start).total_seconds() for start, end in merge_running_intervals(intervals)),
        summed_running_seconds=sum(max(0.0, (item.end - item.start).total_seconds()) for item in intervals),
        interval_count=len(intervals),
        incomplete_interval_count=sum(item.status == "incomplete" for item in intervals),
    )
    return result


def _cost(events: pd.DataFrame) -> dict[str, Any]:
    from .cost import (
        OFFICIAL_PRICES, PRICE_DATE, _has_cost_breakdown,
        calculate_model_event_costs, official_price_history,
    )

    rows = []
    histories = official_price_history()
    for model, group in events.groupby("model", sort=True):
        price = OFFICIAL_PRICES.get(model)
        eligible = group.loc[_has_cost_breakdown(group)]
        priced_count, priced_tokens, estimate = 0, 0, None
        if price is not None and not eligible.empty:
            calculated = calculate_model_event_costs(eligible, model)
            estimate = float(calculated["total_cost_usd"].sum())
            priced_count = len(eligible)
            priced_tokens = int(pd.concat([
                eligible["total_tokens"], eligible["input_tokens"] + eligible["output_tokens"],
            ], axis=1).min(axis=1).sum())
        rows.append({
            "model": model, "estimated_cost_usd": estimate,
            "priced_usage_event_count": priced_count,
            "unpriced_usage_event_count": len(group) - priced_count,
            "unpriced_tokens": int(group["total_tokens"].sum()) - priced_tokens,
            "recorded_price": asdict(price) if price else None,
            "official_price_history": histories.get(model, []),
        })
    priced = [row["estimated_cost_usd"] for row in rows if row["estimated_cost_usd"] is not None]
    return {
        "currency": "USD", "price_date": PRICE_DATE,
        "estimated_cost_usd": sum(priced) if priced else None,
        "unpriced_usage_event_count": sum(row["unpriced_usage_event_count"] for row in rows),
        "unpriced_tokens": sum(row["unpriced_tokens"] for row in rows),
        "models": rows,
        "assumptions": [
            "Estimate using this tool's recorded API prices and long-context rules; not an actual bill.",
            "Prices are selected for each event by its UTC timestamp; the baseline covers earlier or unavailable timestamps.",
            "Unknown models, missing input/output breakdowns and tool-call charges are not priced.",
            "Cached input is a subset of input; reasoning output is a subset of output.",
        ],
    }


def query_usage(
    codex_home: Path | None = None,
    *,
    session_ids: Iterable[str] = (),
    thread_ids: Iterable[str] = (),
    turn_ids: Iterable[str] = (),
    models: Iterable[str] = (),
    include_descendants: bool = False,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    timezone: str = "UTC",
    include_archived: bool = True,
    cache_path: Path | None = None,
    incremental: bool = False,
    progress: bool = False,
    include_cost: bool = False,
) -> dict[str, Any]:
    """Return schema-v1 JSON-compatible data; never render pictures or animations.

    ID lists are exact matches (OR within a list, AND between lists). A thread
    is a root task including all its descendants. Sessions are exact unless
    ``include_descendants`` is enabled. Time is a half-open [start, end) range;
    timestamps without an offset are interpreted in ``timezone``.

    Tokens are selected by event timestamp, while running intervals are clipped
    to the range. With a model filter, only turns whose complete known model set
    is selected contribute runtime; ambiguous/unknown intervals are diagnosed.
    Empty/missing local logs never establish zero server-side usage.
    """
    requested = {
        "session_ids": _ids(session_ids, "session_ids"),
        "thread_ids": _ids(thread_ids, "thread_ids"),
        "turn_ids": _ids(turn_ids, "turn_ids"),
        "models": _ids(models, "models"),
    }
    target_tz = resolve_timezone(timezone)
    lower, upper = _instant(start, target_tz, "start"), _instant(end, target_tz, "end")
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("start 必须早于 end（结束时间不包含在统计范围内）。")
    home = Path(codex_home or default_codex_home()).expanduser().resolve()
    if not home.is_dir():
        raise ValueError(f"Codex 数据目录不存在：{home}")
    if incremental and cache_path is None:
        cache_path = Path(__file__).resolve().parents[1] / "codex_usage_query_cache.sqlite3"
    inventory: dict[str, Any] = {}
    all_events, all_intervals, stats = collect_usage_data(
        home, target_tz, include_archived, progress,
        cache_path=Path(cache_path) if cache_path is not None else None,
        incremental=incremental, inventory=inventory,
    )
    metadata = inventory.get("sessions", {})
    parents = dict(inventory.get("parents", {}))
    turn_models: dict[tuple[str, str], set[str]] = defaultdict(set)
    turn_efforts: dict[tuple[str, str], set[str]] = defaultdict(set)
    unknown_model_turns: set[tuple[str, str]] = set()
    for turn in inventory.get("turns", []):
        key = (turn["session_id"], turn["turn_id"])
        turn_models[key].update(turn.get("models", []))
        turn_efforts[key].update(turn.get("reasoning_efforts", []))
    for event in all_events.to_dict(orient="records"):
        key = (event["session_id"], event["turn_id"])
        if event["model"] and event["model"] != "unknown":
            turn_models[key].add(event["model"])
        else:
            unknown_model_turns.add(key)
        if event.get("reasoning_effort"):
            turn_efforts[key].add(event["reasoning_effort"])
        if event.get("parent_thread_id"):
            parents[event["session_id"]] = event["parent_thread_id"]
    roots = resolve_thread_roots(parents)

    def root(session: str) -> str:
        return roots.get(session, session)

    known_sessions = set(metadata) | set(all_events["session_id"]) | {item.session_id for item in all_intervals}
    known_sessions.discard("")
    known_turns = set(turn_models) | {(item.session_id, item.turn_id) for item in all_intervals if item.turn_id}
    known = {
        "session_ids": known_sessions,
        "thread_ids": {root(session) for session in known_sessions},
        "turn_ids": {turn for _session, turn in known_turns if turn},
        "models": set(all_events["model"]) | {model for values in turn_models.values() for model in values},
    }
    session_selection = set(requested["session_ids"])
    if include_descendants:
        session_selection = _descendants(session_selection, parents)
    model_selection = set(requested["models"])

    def identity_matches(session: str, turn: str | None = None) -> bool:
        return (
            (not requested["session_ids"] or session in session_selection)
            and (not requested["thread_ids"] or root(session) in requested["thread_ids"])
            and (turn is None or not requested["turn_ids"] or turn in requested["turn_ids"])
        )

    mask = pd.Series(True, index=all_events.index)
    if requested["session_ids"]:
        mask &= all_events["session_id"].isin(session_selection)
    if requested["thread_ids"]:
        mask &= all_events["thread_id"].isin(requested["thread_ids"])
    if requested["turn_ids"]:
        mask &= all_events["turn_id"].isin(requested["turn_ids"])
    if model_selection:
        mask &= all_events["model"].isin(model_selection)
    if lower is not None:
        mask &= all_events["timestamp"] >= lower
    if upper is not None:
        mask &= all_events["timestamp"] < upper
    events = all_events.loc[mask].copy()
    if "reasoning_effort" not in events:
        events["reasoning_effort"] = ""

    intervals, interval_rows, excluded_runtime = [], [], []
    turn_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    incomplete_turns, aborted_turns = [], []
    for item in all_intervals:
        if not identity_matches(item.session_id, item.turn_id):
            continue
        clipped_start = max(item.start, lower) if lower is not None else item.start
        clipped_end = min(item.end, upper) if upper is not None else item.end
        if clipped_end < clipped_start or (upper is not None and clipped_start >= upper) or (lower is not None and item.end <= lower and item.start < item.end):
            continue
        key = (item.session_id, item.turn_id)
        associated_models = turn_models[key]
        if model_selection and associated_models and not associated_models & model_selection:
            continue
        runtime_included = not model_selection or (
            bool(associated_models) and associated_models.issubset(model_selection)
            and key not in unknown_model_turns
        )
        turn_statuses[key].add(item.status)
        status_record = {"session_id": item.session_id, "turn_id": item.turn_id or None,
                         "runtime_included": runtime_included}
        if item.status == "incomplete":
            incomplete_turns.append(status_record)
        elif item.status == "aborted":
            aborted_turns.append(status_record)
        if not runtime_included:
            excluded_runtime.append({
                "session_id": item.session_id, "turn_id": item.turn_id,
                "models": sorted(associated_models), "status": item.status,
                "timing_source": item.timing_source,
                "observed_start": item.start.astimezone(target_tz).isoformat(),
                "observed_end": item.end.astimezone(target_tz).isoformat(),
                "reason": "unknown_turn_model" if not associated_models or key in unknown_model_turns else "mixed_turn_models",
            })
            continue
        clipped = replace(item, start=clipped_start, end=clipped_end)
        intervals.append(clipped)
        interval_rows.append({
            "session_id": item.session_id, "thread_id": root(item.session_id),
            "turn_id": item.turn_id, "status": item.status,
            "timing_source": item.timing_source, "models": sorted(associated_models),
            "start": clipped_start.astimezone(target_tz).isoformat(),
            "end": clipped_end.astimezone(target_tz).isoformat(),
            "observed_start": item.start.astimezone(target_tz).isoformat(),
            "observed_end": item.end.astimezone(target_tz).isoformat(),
            "duration_seconds": (clipped_end - clipped_start).total_seconds(),
        })

    event_keys = {(row.session_id, row.turn_id) for row in events.itertuples()}
    selected_keys = event_keys | {(item.session_id, item.turn_id) for item in intervals}
    # Without a time bound, metadata-only turns and sessions are useful matches:
    # they distinguish a known invocation with no usage from an unknown ID.
    if lower is None and upper is None:
        selected_keys |= {
            key for key in known_turns if identity_matches(*key)
            and (not model_selection or bool(turn_models[key] & model_selection))
        }
    selected_sessions = {session for session, _turn in selected_keys if session}
    if not requested["turn_ids"] and not model_selection and lower is None and upper is None:
        selected_sessions |= {session for session in known_sessions if identity_matches(session)}

    session_intervals: dict[str, list[RunningInterval]] = defaultdict(list)
    key_intervals: dict[tuple[str, str], list[RunningInterval]] = defaultdict(list)
    for item in intervals:
        session_intervals[item.session_id].append(item)
        key_intervals[(item.session_id, item.turn_id)].append(item)
    event_groups = {session: group for session, group in events.groupby("session_id", sort=False)}
    turn_groups = {key: group for key, group in events.groupby(["session_id", "turn_id"], sort=False)}
    empty_events = events.iloc[:0]
    session_rows = []
    for session in sorted(selected_sessions):
        group = event_groups.get(session, empty_events)
        keys = [key for key in selected_keys if key[0] == session]
        session_rows.append({
            "session_id": session, "thread_id": root(session),
            "parent_thread_id": parents.get(session) or None,
            "parent_turn_id": metadata.get(session, {}).get("parent_turn_id") or None,
            "models": sorted({model for key in keys for model in turn_models[key]}),
            "reasoning_efforts": sorted({effort for key in keys for effort in turn_efforts[key]}),
            **_totals(group, session_intervals[session]),
        })
    turn_rows = []
    for key in sorted(selected_keys):
        session, turn = key
        if not turn:
            continue
        group = turn_groups.get(key, empty_events)
        turn_rows.append({
            "session_id": session, "thread_id": root(session), "turn_id": turn,
            "models": sorted(turn_models[key]), "reasoning_efforts": sorted(turn_efforts[key]),
            "statuses": sorted(turn_statuses[key]),
            **_totals(group, key_intervals[key]),
        })
    matched = {
        "session_ids": selected_sessions,
        "thread_ids": {root(session) for session in selected_sessions},
        "turn_ids": {turn for _session, turn in selected_keys if turn},
        "models": set(events["model"]) | {model for key in selected_keys for model in turn_models[key] if not model_selection or model in model_selection},
    }
    missing_runtime = [
        {"session_id": session, "turn_id": turn or None}
        for session, turn in sorted(event_keys) if not key_intervals[(session, turn)]
    ]
    # A copied turn can retain tokens in the first rollout while its completed
    # timing record wins in another rollout. Preserve global deduplication and
    # expose the owners instead of assigning that history to both sessions.
    owners: dict[str, set[str]] = defaultdict(set)
    usage_owners: dict[str, set[str]] = defaultdict(set)
    runtime_owners: dict[str, set[str]] = defaultdict(set)
    for session, turn in known_turns:
        if turn:
            owners[turn].add(session)
    for event in all_events.itertuples():
        usage_owners[event.turn_id].add(event.session_id)
    for interval in all_intervals:
        runtime_owners[interval.turn_id].add(interval.session_id)
    selected_turn_ids = {turn for _session, turn in selected_keys if turn}
    ownership_ambiguities = [
        {"turn_id": turn, "observed_session_ids": sorted(owners[turn]),
         "usage_owner_session_ids": sorted(usage_owners[turn]),
         "runtime_owner_session_ids": sorted(runtime_owners[turn])}
        for turn in sorted(selected_turn_ids) if len(owners[turn]) > 1
    ]
    total = _totals(events, intervals)
    total.update(session_count=len(session_rows), turn_count=len(turn_rows),
                 thread_count=len(matched["thread_ids"]), model_count=int(events["model"].nunique()))
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=3, check=False,
        )
        git_revision = revision.stdout.strip() if revision.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        git_revision = None
    result = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "codex_usage", "version": __version__, "git_revision": git_revision, "parser_cache_version": CACHE_VERSION},
        "scope": "Local retained Codex logs only; not server-side account usage or remaining quota.",
        "generated_at": datetime.now(UTC).isoformat(),
        "filters": {
            "codex_home": str(home), "timezone": timezone, **requested,
            "include_descendants": include_descendants, "include_archived": include_archived,
            "start": lower.astimezone(target_tz).isoformat() if lower else None,
            "end": upper.astimezone(target_tz).isoformat() if upper else None,
            "time_range": "[start, end)",
            "cache_path": str(Path(cache_path).expanduser().resolve()) if cache_path is not None else None,
            "incremental": incremental,
        },
        "summary": total, "sessions": session_rows, "turns": turn_rows,
        "events": _records(events[EVENT_COLUMNS]), "running_intervals": interval_rows,
        "matches": {key: sorted(values) for key, values in matched.items()},
        "diagnostics": {
            "complete": stats.files_failed == 0 and stats.malformed_lines == 0,
            "unmatched_ids": {key: [value for value in values if value not in known[key]] for key, values in requested.items()},
            "filtered_out_ids": {key: [value for value in values if value in known[key] and value not in matched[key]] for key, values in requested.items()},
            "sessions_without_usage": [row["session_id"] for row in session_rows if row["usage_event_count"] == 0],
            "turns_without_usage": [{"session_id": row["session_id"], "turn_id": row["turn_id"]} for row in turn_rows if row["usage_event_count"] == 0],
            "missing_runtime": missing_runtime,
            "excluded_runtime": excluded_runtime,
            "ownership_ambiguities": ownership_ambiguities,
            "incomplete_turns": incomplete_turns,
            "aborted_turns": aborted_turns,
            "events_without_session_id": int((events["session_id"] == "").sum()),
            "events_without_turn_id": int((events["turn_id"] == "").sum()),
            "events_without_model": int((events["model"] == "unknown").sum()),
            "parse_stats": asdict(stats),
            "notes": [
                "Usage events are deduplicated local token_count records, not verified server request counts.",
                "Missing usage/timing is not imputed; zero totals describe observed records only.",
                "Runtime uses whole turns clipped to the window, not model processing or reranking latency.",
                "Parent turn IDs are reported only when explicitly present; a parent turn filter does not select child turns.",
                "Copied turns retain global deduplication owners; consult ownership_ambiguities before attributing usage to a particular session.",
            ],
        },
    }
    if include_cost:
        result["cost"] = _cost(events)
    return result
