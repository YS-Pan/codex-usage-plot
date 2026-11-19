"""Transactional metadata cache and byte checkpoints for local rollout logs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .tokens import COMPOSITION_COLUMNS, RAW_TOKEN_COLUMNS, token_composition


# Bump when parsing or persisted metadata semantics change.
CACHE_VERSION = 4
CHECKPOINT_BYTES = 4096
# Avoid the compact-layout cascade-delete regression measured with SQLite's
# default ~2 MiB page cache. Normal spilling and durability remain enabled.
PAGE_CACHE_KIB = 8192
_TABLES = {"metadata", "logs", "events", "intervals"}
_LEGACY_COLUMNS = {
    "metadata": ("key", "value"),
    "logs": ("log_file", "size", "mtime_ns", "file_id", "offset", "head_hash", "tail_hash", "state", "stats"),
    "events": ("sequence", "log_file", "record", "fingerprint"),
    "intervals": ("sequence", "log_file", "record"),
}
_REDUNDANT_EVENT_FIELDS = frozenset((*COMPOSITION_COLUMNS, "log_file", "title"))
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS logs (
        log_file TEXT PRIMARY KEY, size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, file_id TEXT NOT NULL,
        offset INTEGER NOT NULL, head_hash TEXT NOT NULL,
        tail_hash TEXT NOT NULL, state TEXT NOT NULL, stats TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        sequence INTEGER PRIMARY KEY, log_file TEXT NOT NULL,
        record TEXT NOT NULL,
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
)


class CacheInvalidError(ValueError):
    pass


class CacheMigrationError(OSError):
    """A failed upgrade must leave the old cache available, without rescanning."""


def compact_event(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items()
            if key not in _REDUNDANT_EVENT_FIELDS}


@contextmanager
def migration_guard(path: Path):
    """An OS lock releases on process death; the one-byte file may remain."""
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise CacheMigrationError("缓存正在升级，请稍后重试。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            release()


def restore_event(log_file: str, text: str, codex_home: str) -> tuple[dict[str, Any], tuple]:
    """Restore only redundant representations; require every original counter."""
    record = json.loads(text)
    if not isinstance(record, dict) or any(
        type(record.get(key)) is not int or record[key] < 0 for key in RAW_TOKEN_COLUMNS
    ):
        raise ValueError("Invalid raw token counters")
    if any(not isinstance(record.get(key), str)
           for key in ("turn_id", "thread_id", "session_id", "model")):
        raise ValueError("Invalid token identity")
    timestamp = record["timestamp"]
    record["timestamp"] = datetime.fromisoformat(timestamp)
    record["log_file"] = log_file
    record.update(token_composition(record))
    identity = (record["turn_id"] or record["thread_id"] or record["session_id"]
                or str(Path(codex_home) / log_file))
    fingerprint = (identity, timestamp, record["model"],
                   *(record[key] for key in RAW_TOKEN_COLUMNS))
    return record, fingerprint


def restore_legacy_event(row: sqlite3.Row, codex_home: str) -> tuple[dict[str, Any], tuple]:
    record = json.loads(row["record"])
    record["log_file"] = row["log_file"]
    record["timestamp"] = datetime.fromisoformat(record["timestamp"])
    fingerprint = json.loads(row["fingerprint"])
    if not isinstance(fingerprint, list) or any(
        not isinstance(value, (str, int)) for value in fingerprint
    ):
        raise ValueError("Invalid token fingerprint")
    if not any(record.get(key) for key in ("turn_id", "thread_id", "session_id")):
        fingerprint[0] = str(Path(codex_home) / row["log_file"])
    return record, tuple(fingerprint)


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
        source_identity = file_identity(self.path.stat()) if self.path.exists() else None
        connection = sqlite3.connect(self.path, timeout=5)
        self.connection = connection
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA cache_size = -{PAGE_CACHE_KIB}")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ) if not row[0].startswith("sqlite_")
            }
            if tables == _TABLES and version == 3:
                if any(tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})")) != columns
                       for table, columns in _LEGACY_COLUMNS.items()):
                    raise CacheInvalidError("Invalid legacy usage-cache schema")
                if source_identity != file_identity(self.path.stat()):
                    raise CacheMigrationError("缓存已由另一进程升级，请重试。")
                self._migrate_v3(connection, source_identity)
                connection = sqlite3.connect(self.path, timeout=5)
                self.connection = connection
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(f"PRAGMA cache_size = -{PAGE_CACHE_KIB}")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if tables and (version != CACHE_VERSION or tables != _TABLES):
                raise CacheInvalidError("Unsupported usage-cache format")
            for statement in _SCHEMA:
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
            if self.connection is not None:
                self.connection.rollback()
                self.connection.close()
            self.connection = None
            raise

    def _migrate_v3(self, source: sqlite3.Connection, source_identity: str | None) -> None:
        """Convert a stable old snapshot, verify every row, then switch in a transaction.

        BEGIN IMMEDIATE on source excludes writers while a second read-only
        connection takes the SQLite backup. Once complete, that old-format
        backup is retained after success or failure. Interrupted conversion
        never turns a valid legacy cache into an invalid-cache rescan.
        """
        with migration_guard(self.path.with_name(self.path.name + ".migrate.lock")):
            if source_identity != file_identity(self.path.stat()):
                raise CacheMigrationError("缓存已由另一进程升级，请重试。")
            self._convert_v3(source)

    def _convert_v3(self, source: sqlite3.Connection) -> None:
        candidate: Path | None = None
        backup = self.path.with_name(f"{self.path.name}.v3-{datetime.now():%Y%m%d%H%M%S%f}.bak")
        backup_staged = backup.with_suffix(".incomplete")
        try:
            with tempfile.NamedTemporaryFile(prefix=self.path.name + ".migrating-", suffix=".sqlite3",
                                             dir=self.path.parent, delete=False) as handle:
                candidate = Path(handle.name)
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
                with closing(sqlite3.connect(backup_staged)) as saved:
                    reader.backup(saved)
                    if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise CacheInvalidError("Invalid legacy cache backup")
            backup_staged.replace(backup)
            with closing(sqlite3.connect(candidate)) as target:
                target.row_factory = sqlite3.Row
                target.execute("PRAGMA foreign_keys = ON")
                for statement in _SCHEMA:
                    target.execute(statement)
                for table in ("metadata", "logs"):
                    rows = source.execute(f"SELECT * FROM {table} ORDER BY rowid")
                    marks = ",".join("?" for _ in rows.description)
                    target.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

                def events():
                    for row in source.execute("SELECT * FROM events ORDER BY sequence"):
                        try:
                            record = json.loads(row["record"])
                            yield row["sequence"], row["log_file"], json_text(compact_event(record))
                        except (KeyError, TypeError, ValueError, AttributeError) as exc:
                            raise CacheInvalidError("Invalid cached token event") from exc

                def intervals():
                    for row in source.execute("SELECT * FROM intervals ORDER BY sequence"):
                        try:
                            record = json.loads(row["record"])
                            record.pop("log_file", None)
                            yield row["sequence"], row["log_file"], json_text(record)
                        except (KeyError, TypeError, ValueError, AttributeError) as exc:
                            raise CacheInvalidError("Invalid cached running interval") from exc

                target.executemany("INSERT INTO events VALUES (?, ?, ?)", events())
                target.executemany("INSERT INTO intervals VALUES (?, ?, ?)", intervals())
                target.execute(f"PRAGMA user_version = {CACHE_VERSION}")
                target.commit()
                self._verify_migration(source, target)
            self._install_migration(source, candidate, backup)
            source.close()
            self.connection = None
        except (CacheInvalidError, CacheMigrationError, KeyboardInterrupt, SystemExit):
            raise
        except sqlite3.IntegrityError as exc:
            raise CacheInvalidError("Invalid legacy usage-cache relationships") from exc
        except Exception as exc:
            raise CacheMigrationError(f"缓存升级未完成，未触发日志重扫；恢复副本（若已完成）：{backup}") from exc
        finally:
            # Release an attached candidate before deleting it on Windows,
            # including interruptions during the transactional table switch.
            try:
                source.rollback()
                source.execute("DETACH DATABASE upgraded")
            except sqlite3.Error:
                pass
            if candidate is not None:
                candidate.unlink(missing_ok=True)
            backup_staged.unlink(missing_ok=True)

    def _install_migration(self, source: sqlite3.Connection, candidate: Path, backup: Path) -> None:
        # Keep the SQLite writer lock through the switch. Closing the old file
        # before os.replace would let a legacy client commit into the gap and
        # lose that successful update, especially on Windows.
        source.execute("ATTACH DATABASE ? AS upgraded", (str(candidate),))
        self._copy_migration_tables(source)
        source.commit()
        try:
            source.execute("DETACH DATABASE upgraded")
            # The transaction above makes the validated format visible at
            # once. VACUUM then releases the old representation's free pages.
            self._compact_after_migration(source)
        except (sqlite3.Error, OSError) as exc:
            raise CacheMigrationError(
                f"缓存格式已升级，但空间整理未完成；有效缓存和旧格式恢复副本已保留：{backup}"
            ) from exc

    def _copy_migration_tables(self, source: sqlite3.Connection) -> None:
        source.execute("DROP TABLE events")
        source.execute("DROP TABLE intervals")
        for statement in _SCHEMA:
            source.execute(statement)
        source.execute("INSERT INTO events SELECT * FROM upgraded.events")
        source.execute("INSERT INTO intervals SELECT * FROM upgraded.intervals")
        source.execute(f"PRAGMA user_version = {CACHE_VERSION}")

    def _compact_after_migration(self, source: sqlite3.Connection) -> None:
        source.execute("VACUUM")

    def _verify_migration(self, source: sqlite3.Connection, target: sqlite3.Connection) -> None:
        from itertools import zip_longest
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok" or target.execute(
            "PRAGMA foreign_key_check"
        ).fetchone() is not None:
            raise CacheMigrationError("缓存升级校验失败")
        for table in ("metadata", "logs"):
            if any(tuple(old) != tuple(new) for old, new in zip_longest(
                source.execute(f"SELECT * FROM {table} ORDER BY rowid"),
                target.execute(f"SELECT * FROM {table} ORDER BY rowid"), fillvalue=(),
            )):
                raise CacheMigrationError(f"缓存升级检查点不一致：{table}")
        for old, new in zip_longest(source.execute("SELECT * FROM events ORDER BY sequence"),
                                    target.execute("SELECT * FROM events ORDER BY sequence")):
            if old is None or new is None or tuple(old)[:2] != tuple(new)[:2]:
                raise CacheMigrationError("缓存升级事件顺序不一致")
            try:
                expected = restore_legacy_event(old, self.codex_home)
                actual = restore_event(new["log_file"], new["record"], self.codex_home)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise CacheInvalidError("Invalid cached token event") from exc
            if actual != expected:
                raise CacheMigrationError("缓存升级事件还原不一致")
        for old, new in zip_longest(source.execute("SELECT * FROM intervals ORDER BY sequence"),
                                    target.execute("SELECT * FROM intervals ORDER BY sequence")):
            if old is None or new is None or tuple(old)[:2] != tuple(new)[:2]:
                raise CacheMigrationError("缓存升级运行区间顺序不一致")
            expected, actual = json.loads(old["record"]), json.loads(new["record"])
            expected["log_file"] = actual["log_file"] = old["log_file"]
            if actual != expected:
                raise CacheMigrationError("缓存升级运行区间还原不一致")

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
                if any(not isinstance(entry["state"].get(key), str) for key in ("model", "turn_id", "reasoning_effort")):
                    raise ValueError("Invalid model or turn ID")
                turns = entry["state"].get("turns")
                if not isinstance(turns, list) or any(
                    not isinstance(turn, dict)
                    or any(not isinstance(turn.get(key), str) for key in ("session_id", "turn_id"))
                    or any(not isinstance(turn.get(key), list)
                           or any(not isinstance(value, str) for value in turn[key])
                           for key in ("models", "reasoning_efforts"))
                    for turn in turns
                ):
                    raise ValueError("Invalid turn inventory")
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
                stored = compact_event(record)
                stored["timestamp"] = record["timestamp"].isoformat()
                yield log_file, json_text(stored)
        self.connection.executemany(
            "INSERT INTO events(log_file, record) VALUES (?, ?)", rows()
        )

    def save_intervals(self, log_file: str, intervals: Iterable[Any]) -> None:
        from dataclasses import asdict
        assert self.connection is not None
        self.connection.executemany(
            "INSERT INTO intervals(log_file, record) VALUES (?, ?)",
            ((log_file, json_text({**{key: value for key, value in asdict(item).items() if key != "log_file"}, "start": item.start.isoformat(),
                                 "end": item.end.isoformat()})) for item in intervals),
        )

    def load_events(self, active: set[str]) -> Iterable[tuple[dict[str, Any], tuple]]:
        assert self.connection is not None
        with closing(self.connection.execute(
            "SELECT log_file, record FROM events ORDER BY log_file COLLATE NOCASE, sequence"
        )) as rows:
            for row in rows:
                if row[0] in active:
                    try:
                        yield restore_event(row[0], row[1], self.codex_home)
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
