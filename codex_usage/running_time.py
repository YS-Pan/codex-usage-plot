"""Measure merged and summed running time using only rollout timing metadata."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable

import pandas as pd


@dataclass
class RuntimeParseStats:
    starts_seen: int = 0
    ends_seen: int = 0
    invalid_intervals: int = 0
    starts_recovered_from_duration: int = 0
    duplicate_intervals_removed: int = 0
    incomplete_intervals: int = 0


@dataclass(frozen=True)
class RunningInterval:
    start: datetime
    end: datetime
    session_id: str = ""
    turn_id: str = ""
    status: str = "completed"
    timing_source: str = "boundaries"
    log_file: str = ""


@dataclass
class OpenTurn:
    start: datetime
    last_seen: datetime
    turn_id: str


def timing_timestamp(value: object) -> datetime | None:
    """Accept ISO timestamps and Codex's Unix-second lifecycle timestamps."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


class RunningIntervalTracker:
    """Track one rollout without retaining messages or tool output bodies."""

    def __init__(self, log_file: str, stats: RuntimeParseStats) -> None:
        self.log_file = log_file
        self.stats = stats
        self.session_id = ""
        self.active: OpenTurn | None = None
        self.intervals: list[RunningInterval] = []

    def checkpoint(self) -> dict[str, Any]:
        """Persist only the timing state needed to resume an unfinished turn."""
        return {
            "session_id": self.session_id,
            "active": {
                "start": self.active.start.isoformat(),
                "last_seen": self.active.last_seen.isoformat(),
                "turn_id": self.active.turn_id,
            } if self.active is not None else None,
        }

    def restore_checkpoint(self, state: dict[str, Any]) -> None:
        self.session_id = state.get("session_id", "")
        active = state.get("active")
        if active is not None:
            self.active = OpenTurn(
                datetime.fromisoformat(active["start"]),
                datetime.fromisoformat(active["last_seen"]), active["turn_id"],
            )

    def finish_open_turn(self) -> None:
        if self.active is not None:
            self.intervals.append(
                RunningInterval(
                    self.active.start,
                    self.active.last_seen,
                    self.session_id,
                    self.active.turn_id,
                    "incomplete",
                    "last_observed_event",
                    self.log_file,
                )
            )
            self.active = None

    def observe(
        self, event_type: str, payload: dict[str, Any], timestamp_value: object
    ) -> None:
        if event_type == "session_meta":
            self.session_id = str(payload.get("id") or payload.get("session_id") or "")
            return

        kind = payload.get("type") if event_type == "event_msg" else None
        turn_id = payload.get("turn_id")
        turn_id = turn_id if isinstance(turn_id, str) else ""

        if kind == "task_started":
            self.stats.starts_seen += 1
            start = timing_timestamp(payload.get("started_at")) or timing_timestamp(
                timestamp_value
            )
            if self.active is not None and turn_id and turn_id == self.active.turn_id:
                return  # Replayed start marker for the same running turn.
            # A new turn must not extend an abandoned previous turn through the gap.
            self.finish_open_turn()
            if start is None:
                self.stats.invalid_intervals += 1
            else:
                self.active = OpenTurn(start, start, turn_id)
            return

        if kind in ("task_complete", "turn_aborted"):
            self.stats.ends_seen += 1
            active = self.active
            matches_active = active is not None and (
                not turn_id or not active.turn_id or turn_id == active.turn_id
            )
            start = timing_timestamp(payload.get("started_at"))
            if start is None and matches_active:
                start = active.start
            end = timing_timestamp(payload.get("completed_at")) or timing_timestamp(
                timestamp_value
            )
            duration = payload.get("duration_ms")
            has_duration = (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration >= 0
            )
            source = "boundaries"
            try:
                if has_duration and start is not None:
                    # Preserve the recorded elapsed time; Unix timestamps alone
                    # are rounded to seconds and log writes can arrive late.
                    end = start + timedelta(milliseconds=duration)
                    source = "recorded_duration"
                elif has_duration and end is not None:
                    start = end - timedelta(milliseconds=duration)
                    source = "duration_backfill"
                    self.stats.starts_recovered_from_duration += 1
            except OverflowError:
                start = None

            if matches_active:
                self.active = None
                turn_id = turn_id or active.turn_id
            if start is None or end is None or end < start:
                self.stats.invalid_intervals += 1
                return
            self.intervals.append(
                RunningInterval(
                    start,
                    end,
                    self.session_id,
                    turn_id,
                    "aborted" if kind == "turn_aborted" else "completed",
                    source,
                    self.log_file,
                )
            )
            return

        if self.active is None:
            return
        identifies_turn = event_type == "turn_context" or kind == "token_count"
        if identifies_turn and turn_id and not self.active.turn_id:
            self.active.turn_id = turn_id
        if identifies_turn and turn_id and turn_id != self.active.turn_id:
            self.finish_open_turn()
            return
        # Settings changes and other out-of-turn metadata are not evidence that
        # an unfinished turn was still executing.
        is_activity = event_type in ("turn_context", "response_item", "compacted") or (
            event_type == "event_msg"
            and kind in (
                "user_message", "agent_message", "agent_reasoning", "token_count",
                "item_completed", "patch_apply_end", "web_search_end",
                "mcp_tool_call_end", "context_compacted",
            )
        )
        if is_activity:
            timestamp = timing_timestamp(timestamp_value)
            if timestamp is not None:
                self.active.last_seen = max(self.active.last_seen, timestamp)


def deduplicate_running_intervals(
    intervals: Iterable[RunningInterval], stats: RuntimeParseStats
) -> list[RunningInterval]:
    """Prefer a completed record over an unfinished copy of the same turn."""
    unique: dict[tuple[object, ...], RunningInterval] = {}
    for interval in intervals:
        key = ("turn", interval.turn_id) if interval.turn_id else (
            "interval", interval.session_id or interval.log_file,
            interval.start, interval.end, interval.status,
        )
        previous = unique.get(key)
        if previous is not None:
            stats.duplicate_intervals_removed += 1

            def rank(value: RunningInterval) -> tuple[bool, bool, datetime]:
                return (
                    value.status != "incomplete",
                    value.timing_source != "duration_backfill",
                    value.end,
                )

            if rank(previous) >= rank(interval):
                continue
        unique[key] = interval
    result = sorted(unique.values(), key=lambda interval: interval.start)
    stats.incomplete_intervals = sum(item.status == "incomplete" for item in result)
    return result


def merge_running_intervals(
    intervals: Iterable[RunningInterval],
) -> list[tuple[datetime, datetime]]:
    """Return a union of half-open UTC intervals, including touching intervals."""
    ordered = []
    for item in intervals:
        start, end = item.start.astimezone(timezone.utc), item.end.astimezone(timezone.utc)
        if end > start:
            ordered.append((start, end))
    ordered.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def local_midnight(day: date, target_tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=target_tz)


def aggregate_daily_running_time(
    intervals: Iterable[RunningInterval],
    target_tz: tzinfo,
    start: date,
    end: date,
    observed_at: datetime | None = None,
) -> pd.DataFrame:
    """Split canonical turn intervals into daily union and summed durations."""
    if start > end:
        raise ValueError("Runtime start date must not be later than end date")
    observed_at = observed_at or datetime.now(timezone.utc)
    intervals = list(intervals)
    dates = pd.date_range(local_midnight(start, target_tz), local_midnight(end, target_tz))
    lower = local_midnight(start, target_tz).astimezone(timezone.utc)
    upper = local_midnight(end + timedelta(days=1), target_tz).astimezone(timezone.utc)

    def seconds_per_day(spans: Iterable[tuple[datetime, datetime]]) -> list[float]:
        seconds = {stamp.date(): 0.0 for stamp in dates}
        for interval_start, interval_end in spans:
            cursor, stop = max(interval_start, lower), min(interval_end, upper, observed_at)
            while cursor < stop:
                day = cursor.astimezone(target_tz).date()
                midnight = local_midnight(day + timedelta(days=1), target_tz).astimezone(
                    timezone.utc
                )
                segment_end = min(stop, midnight)
                seconds[day] += (segment_end - cursor).total_seconds()
                cursor = segment_end
        return list(seconds.values())

    next_midnights = [
        local_midnight(stamp.date() + timedelta(days=1), target_tz).astimezone(timezone.utc)
        for stamp in dates
    ]
    daily = pd.DataFrame({
        "date": dates,
        "running_seconds": seconds_per_day(merge_running_intervals(intervals)),
        # Log copies have already been removed by the collector. Concurrent
        # sessions contribute separately here, so a day's sum can exceed 24 h.
        "summed_running_seconds": seconds_per_day(
            (item.start.astimezone(timezone.utc), item.end.astimezone(timezone.utc))
            for item in intervals
        ),
    })
    daily["running_hours"] = daily["running_seconds"] / 3600
    daily["summed_running_hours"] = daily["summed_running_seconds"] / 3600
    daily["day_seconds"] = [
        (next_midnight - stamp.to_pydatetime().astimezone(timezone.utc)).total_seconds()
        for stamp, next_midnight in zip(dates, next_midnights)
    ]
    daily["running_fraction"] = daily["running_seconds"] / daily["day_seconds"]
    daily["running_percent"] = daily["running_fraction"] * 100
    daily["rolling_7d_running_percent"] = daily["running_percent"].rolling(
        7, min_periods=1
    ).mean()
    daily["rolling_7d_running_hours"] = daily["running_hours"].rolling(
        7, min_periods=1
    ).mean()
    daily["is_complete_day"] = [value <= observed_at for value in next_midnights]
    return daily


def running_intervals_frame(
    intervals: Iterable[RunningInterval], target_tz: tzinfo
) -> pd.DataFrame:
    columns = [
        "start", "end", "session_id", "turn_id", "status", "timing_source", "log_file"
    ]
    frame = pd.DataFrame([asdict(item) for item in intervals], columns=columns)
    for column in ("start", "end"):
        frame[column] = pd.to_datetime(frame[column], utc=True).dt.tz_convert(target_tz)
    frame["duration_seconds"] = (frame["end"] - frame["start"]).dt.total_seconds()
    return frame
