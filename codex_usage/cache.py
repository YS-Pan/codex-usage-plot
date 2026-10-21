"""Transactional metadata cache and byte checkpoints for local rollout logs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Bump when parsing or persisted metadata semantics change.
CACHE_VERSION = 2
CHECKPOINT_BYTES = 4096


class CacheInvalidError(ValueError):
    pass


def file_identity(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""


def checkpoint_hashes(path: Path, offset: int) -> tuple[str, str]:
    """Check small samples of the consumed prefix without rereading history."""
    length = min(offset, CHECKPOINT_BYTES)
    with path.open("rb") as handle:
        head = handle.read(length)
        handle.seek(offset - length)
        tail = handle.read(length)
    if len(head) != length or len(tail) != length:
        raise OSError(f"Log changed while reading: {path}")
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()


def can_resume(path: Path, stat: os.stat_result, entry: dict[str, Any]) -> bool:
    if stat.st_size < entry["size"] or stat.st_size < entry["offset"]:
        return False
    if entry["file_id"] and file_identity(stat) != entry["file_id"]:
        return False
    # A changed file of equal size was edited, rather than appended.
    if stat.st_size == entry["size"] and stat.st_mtime_ns != entry["mtime_ns"]:
        return False
    return checkpoint_hashes(path, entry["offset"]) == (
        entry["head_hash"], entry["tail_hash"]
    )


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class UsageCache:
    """All file updates commit together; interruption preserves the old cursor."""

    def __init__(self, path: Path, codex_home: Path) -> None:
        self.path = path
        self.codex_home = str(codex_home.resolve())
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> UsageCache:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        self.connection = connection
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ) if not row[0].startswith("sqlite_")
            }
            if tables and (version != CACHE_VERSION or tables != {"metadata", "logs", "events", "intervals"}):
                raise CacheInvalidError("Unsupported usage-cache format")
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS logs (
                    log_file TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, file_id TEXT NOT NULL,
                    offset INTEGER NOT NULL, head_hash TEXT NOT NULL,
                    tail_hash TEXT NOT NULL, state TEXT NOT NULL, stats TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, log_file TEXT NOT NULL,
                    record TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    FOREIGN KEY (log_file) REFERENCES logs(log_file)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )""",
                """CREATE TABLE IF NOT EXISTS intervals (
                    sequence INTEGER PRIMARY KEY, log_file TEXT NOT NULL, record TEXT NOT NULL,
                    FOREIGN KEY (log_file) REFERENCES logs(log_file)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS events_log ON events(log_file)",
                "CREATE INDEX IF NOT EXISTS intervals_log ON intervals(log_file)",
                # Match the stable file ordering without sorting all event JSON
                # into a large temporary table on every incremental read.
                "CREATE INDEX IF NOT EXISTS events_read_order ON events(log_file COLLATE NOCASE, sequence)",
                "CREATE INDEX IF NOT EXISTS intervals_read_order ON intervals(log_file COLLATE NOCASE, sequence)",
            ):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {CACHE_VERSION}")
            previous = connection.execute(
                "SELECT value FROM metadata WHERE key = 'codex_home'"
            ).fetchone()
            if previous is None or previous[0] != self.codex_home:
                self.clear()
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('codex_home', ?)", (self.codex_home,)
            )
            return self
        except BaseException:
            connection.rollback()
            connection.close()
            self.connection = None
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self.connection is not None
        try:
            if exc_type is None:
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('updated_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()

    def clear(self) -> None:
        assert self.connection is not None
        self.connection.execute("DELETE FROM logs")

    def entries(self) -> dict[str, dict[str, Any]]:
        assert self.connection is not None
        result = {}
        for row in self.connection.execute("SELECT * FROM logs").fetchall():
            entry = dict(row)
            try:
                entry["state"] = json.loads(entry["state"])
                entry["stats"] = json.loads(entry["stats"])
                if not isinstance(entry["state"], dict) or not isinstance(entry["stats"], dict):
                    raise ValueError("Invalid parser state")
                if not 0 <= entry["offset"] <= entry["size"]:
                    raise ValueError("Invalid byte checkpoint")
                if entry["state"].get("offset") != entry["offset"]:
                    raise ValueError("Inconsistent byte checkpoint")
                if any(not isinstance(entry["state"].get(key), dict) for key in ("meta", "parents", "runtime")):
                    raise ValueError("Invalid parser metadata")
                if any(not isinstance(entry["state"].get(key), str) for key in ("model", "turn_id")):
                    raise ValueError("Invalid model or turn ID")
            except (TypeError, ValueError) as exc:
                raise CacheInvalidError("Invalid usage-cache checkpoint") from exc
            result[entry["log_file"]] = entry
        return result

    def remove(self, log_file: str) -> None:
        assert self.connection is not None
        self.connection.execute("DELETE FROM logs WHERE log_file = ?", (log_file,))

    def move(self, old: str, new: str) -> None:
        assert self.connection is not None
        self.connection.execute("UPDATE logs SET log_file = ? WHERE log_file = ?", (new, old))

    def save_file(self, log_file: str, entry: dict[str, Any]) -> None:
        assert self.connection is not None
        self.connection.execute(
            """INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(log_file) DO UPDATE SET
                 size=excluded.size, mtime_ns=excluded.mtime_ns, file_id=excluded.file_id,
                 offset=excluded.offset, head_hash=excluded.head_hash, tail_hash=excluded.tail_hash,
                 state=excluded.state, stats=excluded.stats""",
            (log_file, entry["size"], entry["mtime_ns"], entry["file_id"], entry["offset"],
             entry["head_hash"], entry["tail_hash"], json_text(entry["state"]), json_text(entry["stats"])),
        )

    def save_events(self, log_file: str, events: Iterable[tuple[dict[str, Any], tuple]]) -> None:
        assert self.connection is not None
        def rows():
            for record, fingerprint in events:
                stored = {**record, "timestamp": record["timestamp"].isoformat()}
                stored.pop("title", None)  # Refresh titles from the index on every run.
                yield log_file, json_text(stored), json_text(fingerprint)
        self.connection.executemany(
            "INSERT INTO events(log_file, record, fingerprint) VALUES (?, ?, ?)", rows()
        )

    def save_intervals(self, log_file: str, intervals: Iterable[Any]) -> None:
        from dataclasses import asdict
        assert self.connection is not None
        self.connection.executemany(
            "INSERT INTO intervals(log_file, record) VALUES (?, ?)",
            ((log_file, json_text({**asdict(item), "start": item.start.isoformat(),
                                 "end": item.end.isoformat()})) for item in intervals),
        )

    def load_events(self, active: set[str]) -> Iterable[tuple[dict[str, Any], tuple]]:
        assert self.connection is not None
        with closing(self.connection.execute(
            "SELECT log_file, record, fingerprint FROM events ORDER BY log_file COLLATE NOCASE, sequence"
        )) as rows:
            for row in rows:
                if row[0] in active:
                    try:
                        record = json.loads(row[1])
                        record["log_file"] = row[0]
                        record["timestamp"] = datetime.fromisoformat(record["timestamp"])
                        fingerprint = json.loads(row[2])
                        if not isinstance(fingerprint, list) or any(
                            not isinstance(value, (str, int)) for value in fingerprint
                        ):
                            raise ValueError("Invalid token fingerprint")
                        if not any(record.get(key) for key in ("turn_id", "thread_id", "session_id")):
                            fingerprint[0] = str(Path(self.codex_home) / row[0])
                        yield record, tuple(fingerprint)
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise CacheInvalidError("Invalid cached token event") from exc

    def load_intervals(self, active: set[str]) -> Iterable[Any]:
        from .running_time import RunningInterval
        assert self.connection is not None
        with closing(self.connection.execute(
            "SELECT log_file, record FROM intervals ORDER BY log_file COLLATE NOCASE, sequence"
        )) as rows:
            for row in rows:
                if row[0] in active:
                    try:
                        record = json.loads(row[1])
                        record["log_file"] = row[0]
                        record["start"] = datetime.fromisoformat(record["start"])
                        record["end"] = datetime.fromisoformat(record["end"])
                        yield RunningInterval(**record)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise CacheInvalidError("Invalid cached running interval") from exc
