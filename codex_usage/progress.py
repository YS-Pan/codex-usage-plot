"""Stage-local progress shared by command-line workers and the Tk window."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import sys
from time import monotonic


PROGRESS_ENV = "CODEX_USAGE_PROGRESS"
PROGRESS_PREFIX = "CODEX_USAGE_PROGRESS "


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    label: str
    completed: int | None = None
    total: int | None = None
    unit: str = ""
    detail: str = ""

    @property
    def percent(self) -> float | None:
        if self.completed is None or not self.total:
            return None
        return self.completed / self.total * 100

    @property
    def counts(self) -> str:
        if self.percent is None:
            return ""
        return f"{self.completed:,}/{self.total:,} {self.unit}（本阶段 {int(self.percent)}%）"

    def text(self) -> str:
        return " · ".join(part for part in (self.label, self.counts, self.detail) if part)


def parse_progress(line: str) -> ProgressEvent | None:
    """Ignore ordinary output and malformed records without breaking a run."""
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        value = json.loads(line[len(PROGRESS_PREFIX):])
        event = ProgressEvent(**value)
        if not all(isinstance(getattr(event, name), str) for name in ("stage", "label", "unit", "detail")):
            return None
        if not event.stage or not event.label:
            return None
        if event.completed is None and event.total is None:
            return event
        if type(event.completed) is not int or type(event.total) is not int:
            return None
        if not 0 <= event.completed <= event.total:
            return None
        return event
    except (ValueError, TypeError):
        return None


class ProgressReporter:
    """Report real completed work, throttling frequent events but never the last."""

    def __init__(self, stage: str, label: str, *, total: int | None = None,
                 unit: str = "", enabled: bool = True) -> None:
        self.stage = stage
        self.label = label
        self.total = total
        self.unit = unit
        self.enabled = enabled
        self.last_update: float | None = None

    def update(self, completed: int | None = None, detail: str = "", *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = monotonic()
        if (not force and completed != self.total and self.last_update is not None
                and now - self.last_update < 0.2):
            return
        self.last_update = now
        event = ProgressEvent(self.stage, self.label, completed, self.total, self.unit, detail)
        if os.environ.get(PROGRESS_ENV) == "json":
            text = PROGRESS_PREFIX + json.dumps(asdict(event), ensure_ascii=False, allow_nan=False)
        else:
            text = event.text()
        print(text, file=sys.stderr, flush=True)


def report_progress(stage: str, label: str, *, detail: str = "", enabled: bool = True) -> None:
    """Use activity-only progress when a stage has no honest countable total."""
    ProgressReporter(stage, label, enabled=enabled).update(detail=detail)


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
