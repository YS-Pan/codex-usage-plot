"""Repeatable, isolated v3/v4 cache comparison; never writes the source database.

The v4 candidate failed the deletion performance gate. Production stays on v3;
this driver explicitly loads research/cache-v4/cache.py for the v4 label.
Results and all databases/reports stay beneath --work; only aggregates print.
The operating-system page cache is not flushed: these are interleaved warm runs.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
from time import perf_counter, process_time
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY))
WORKLOADS = ("read", "incremental", "append", "rebuild", "delete", "archive", "report")


def connection(path: Path, *, readonly=False):
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    return sqlite3.connect(uri, uri=True, timeout=30)


def backup(source: Path, target: Path):
    if target.exists():
        raise FileExistsError(target)
    with closing(connection(source, readonly=True)) as original:
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied)


def module_for(label: str, work: Path):
    if label.startswith("v4"):
        candidate = Path(__file__).with_name("research") / "cache-v4/cache.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("codex_usage._benchmark_v4_candidate", candidate)
            cache = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cache)
        else:
            raise FileNotFoundError(f"缺少已拒绝的 v4 研究候选，停止以免误测正式 v3：{candidate}")
        if "_" in label:
            page_kib = int(label.split("_", 1)[1])
            class BoundedPageCache(cache.UsageCache):
                def __enter__(self):
                    result = super().__enter__()
                    self.connection.execute(f"PRAGMA cache_size = -{page_kib}")
                    return result
            return SimpleNamespace(UsageCache=BoundedPageCache)
        return cache
    spec = importlib.util.spec_from_file_location("codex_usage._benchmark_v3", work / "v3_cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_home(path: Path):
    with closing(connection(path, readonly=True)) as database:
        return Path(database.execute("SELECT value FROM metadata WHERE key='codex_home'").fetchone()[0])


def peak_bytes():
    if os.name != "nt":
        import resource
        amount = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return amount if sys.platform == "darwin" else amount * 1024
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage",
            )
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(kernel.GetCurrentProcess()), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return counters.PeakWorkingSetSize


def process_io():
    """Process file-I/O counters; these are API bytes, not physical-disk bytes."""
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]
    counters = Counters()
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    if not kernel.GetProcessIoCounters(ctypes.c_void_p(kernel.GetCurrentProcess()), ctypes.byref(counters)):
        raise ctypes.WinError()
    return {"write_calls": counters.WriteOperationCount, "write_bytes": counters.WriteTransferCount}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat()).encode("utf-8")


def source_hashes(work):
    paths = {f"codex_usage/{path.name}": path for path in sorted((REPOSITORY / "codex_usage").glob("*.py"))}
    paths["baseline_cache"] = work / "v3_cache.py"
    paths["benchmark_driver"] = Path(__file__).resolve()
    candidate = Path(__file__).with_name("research") / "cache-v4/cache.py"
    if candidate.exists():
        paths["research_candidate_cache"] = candidate
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def inspect_equivalence(work):
    left_module, right_module = [module_for(label, work) for label in ("v3", "v4")]
    left_path, right_path = [work / f"{label}.sqlite3" for label in ("v3", "v4")]
    home = source_home(left_path)
    result = {}
    with left_module.UsageCache(left_path, home) as left, right_module.UsageCache(right_path, home) as right:
        left_entries, right_entries = left.entries(), right.entries()
        if left_entries != right_entries:
            raise AssertionError("Checkpoint/state/statistics mismatch")
        result["checkpoints"] = len(left_entries)
        result["checkpoint_sha256"] = hashlib.sha256(canonical(left_entries)).hexdigest()
        active = set(left_entries)
        for kind in ("events", "intervals"):
            digest, count = hashlib.sha256(), 0
            for first, second in itertools.zip_longest(
                getattr(left, f"load_{kind}")(active), getattr(right, f"load_{kind}")(active)
            ):
                if first != second:
                    raise AssertionError(f"Restored {kind} mismatch at sequence {count}")
                normalized = asdict(first) if kind == "intervals" else first
                digest.update(canonical(normalized))
                count += 1
            result[kind] = count
            result[f"{kind}_sha256"] = digest.hexdigest()
        for table in ("logs", "events", "intervals"):
            query = f"SELECT {'log_file' if table == 'logs' else 'sequence, log_file'} FROM {table} ORDER BY rowid"
            for first, second in itertools.zip_longest(left.connection.execute(query), right.connection.execute(query)):
                if first is None or second is None or tuple(first) != tuple(second):
                    raise AssertionError(f"Stored {table} identity/order mismatch")
        for database in (left.connection, right.connection):
            if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AssertionError("SQLite quick_check failed")
        for database in (left.connection, right.connection):
            indexes = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )}
            if indexes != {"events_log", "events_read_order", "intervals_log", "intervals_read_order"}:
                raise AssertionError("Required indexes changed")
    result["bytes"] = {label: (work / f"{label}.sqlite3").stat().st_size for label in ("v3", "v4")}
    return result


def prepare(args):
    args.work.mkdir(parents=True, exist_ok=True)
    archived_baseline = Path(__file__).with_name("baseline-cache-v3.py")
    if args.revision == "edda407" and archived_baseline.exists():
        baseline = archived_baseline.read_bytes()
    else:
        baseline = subprocess.check_output(
            ["git", "show", f"{args.revision}:codex_usage/cache.py"], cwd=REPOSITORY
        )
    (args.work / "v3_cache.py").write_bytes(baseline)
    backup(args.source, args.work / "v3.sqlite3")
    with closing(connection(args.work / "v3.sqlite3", readonly=True)) as db:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("logs", "events", "intervals")}
    print(json.dumps({"baseline_revision": args.revision, "bytes": args.source.stat().st_size,
                      "counts": counts}, ensure_ascii=False), flush=True)


def migrate(args):
    """Measure the production migration, including verification and compaction."""
    target = args.work / "v4.sqlite3"
    if target.exists():
        raise FileExistsError("Move aside the previous candidate before measuring a new migration")
    shutil.copyfile(args.work / "v3.sqlite3", target)
    module = module_for("v4", args.work)
    before = set(args.work.glob("v4.sqlite3.v3-*.bak"))
    home = source_home(target)
    started = perf_counter()
    with module.UsageCache(target, home):
        pass
    elapsed = perf_counter() - started
    saved = set(args.work.glob("v4.sqlite3.v3-*.bak")) - before
    result = {"seconds": elapsed, "process_peak_working_set_bytes": peak_bytes(),
              "original_active_bytes": (args.work / "v3.sqlite3").stat().st_size,
              "candidate_active_bytes": target.stat().st_size,
              "new_retained_backups": len(saved),
              "new_retained_backup_bytes": sum(path.stat().st_size for path in saved),
              "python": sys.executable}
    (args.work / "migration.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"migration": result}), flush=True)


def verify(args):
    result = inspect_equivalence(args.work)
    (args.work / "equivalence.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"equivalence": result}), flush=True)


def verify_exports(args):
    if len(args.labels) != 2:
        raise ValueError("Export comparison needs exactly two labels")
    folders = [args.work / f"report-{label}" for label in args.labels]
    result = {"csv": {}, "png": {}}
    for suffix in ("csv", "png"):
        names = [{path.name for path in folder.glob(f"*.{suffix}")} for folder in folders]
        if names[0] != names[1] or not names[0]:
            raise AssertionError(f"Exported {suffix} file lists differ or are empty")
        for name in sorted(names[0]):
            hashes = {}
            for label, folder in zip(args.labels, folders):
                digest = hashlib.sha256()
                with (folder / name).open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                hashes[label] = digest.hexdigest()
            equal = len(set(hashes.values())) == 1
            result[suffix][name] = {"equal_bytes": equal, "sha256": hashes}
            if suffix == "csv" and not equal:
                raise AssertionError(f"Exported CSV differs: {name}")
    summaries = [json.loads((folder / "summary.json").read_text(encoding="utf-8")) for folder in folders]
    for summary in summaries:
        summary.pop("generated_at", None)
        summary["running_time"].pop("observed_at", None)
        summary["parse_stats"]["scan"].pop("seconds", None)
    result["summary_statistics_equal"] = summaries[0] == summaries[1]
    result["omitted_runtime_fields"] = ["generated_at", "running_time.observed_at", "parse_stats.scan.seconds"]
    if not result["summary_statistics_equal"]:
        raise AssertionError("Report statistical summaries differ after omitting only runtime fields")
    result["normalized_summary_sha256"] = hashlib.sha256(canonical(summaries[0])).hexdigest()
    result["equal_png_count"] = sum(item["equal_bytes"] for item in result["png"].values())
    (args.work / "export-equivalence.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"export_equivalence": result}), flush=True)


class FrozenPath(type(Path())):
    """A discovered pathname with the immutable stat already recorded in the cache."""
    def stat(self, *, follow_symlinks=True):
        return self.snapshot_stat


def frozen_pipeline(module, database, home):
    from codex_usage import charts
    with module.UsageCache(database, home) as cache:
        entries = cache.entries()
    files = []
    for log, entry in sorted(entries.items(), key=lambda pair: pair[0].lower()):
        path = FrozenPath(home / log)
        device, inode = (entry["file_id"].split(":") if entry["file_id"] else ("0", "0"))
        path.snapshot_stat = SimpleNamespace(st_size=entry["size"], st_mtime_ns=entry["mtime_ns"],
                                             st_dev=int(device), st_ino=int(inode))
        files.append(path)
    with patch.object(charts, "UsageCache", module.UsageCache), patch.object(
        charts, "discover_rollouts", return_value=files
    ), patch.object(charts, "load_session_titles", return_value={}):
        events, intervals, stats = charts.collect_cached_usage_data(
            home, timezone.utc, True, False, database, True,
        )
    if stats.scan.bytes_read != 0 or stats.scan.files_reused != len(entries):
        raise AssertionError("Snapshot was unexpectedly parsed")
    return events, intervals, stats


def worker(args):
    if (args.work / "pause-before-next-sample").exists():
        raise SystemExit("Benchmark paused before the next sample.")
    from codex_usage import charts
    module = module_for(args.label, args.work)
    database = args.work / f"working-{args.label}-{args.workload}.sqlite3"
    reusable = args.workload in {"read", "incremental", "report"}
    preparation = "reuse_read_working_copy" if reusable and database.exists() else "copy_immutable_master"
    if not reusable or not database.exists():
        # Masters are closed, verified SQLite snapshots, with no WAL/journal.
        # A byte copy restores mutation trials without repeated backup page I/O.
        # Read-only workloads alter only updated_at, so their working copy can
        # be reopened across trials. None of this preparation is timed.
        master_label = "v4" if args.label.startswith("v4_") else args.label
        shutil.copyfile(args.work / f"{master_label}.sqlite3", database)
    home = source_home(database)
    with module.UsageCache(database, home) as cache:
        entries = cache.entries()
        active = set(entries)
        chosen = cache.connection.execute(
            "SELECT log_file FROM events GROUP BY log_file ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()[0]
        selection = {
            "selected_log_sha256": hashlib.sha256(chosen.encode("utf-8")).hexdigest(),
            "selected_events": cache.connection.execute(
                "SELECT count(*) FROM events WHERE log_file=?", (chosen,)
            ).fetchone()[0],
            "selected_intervals": cache.connection.execute(
                "SELECT count(*) FROM intervals WHERE log_file=?", (chosen,)
            ).fetchone()[0],
            "page_size": cache.connection.execute("PRAGMA page_size").fetchone()[0],
            "cache_size": cache.connection.execute("PRAGMA cache_size").fetchone()[0],
            "freelist_count_before": cache.connection.execute("PRAGMA freelist_count").fetchone()[0],
            "delete_query_plan": [row[3] for row in cache.connection.execute(
                "EXPLAIN QUERY PLAN DELETE FROM logs WHERE log_file=?", (chosen,)
            )],
        }
        if args.workload in {"append", "rebuild"}:
            if args.workload == "append":
                events = list(itertools.islice(cache.load_events(active), 4096))
                intervals = []
            else:
                events = list(cache.load_events({chosen}))
                intervals = list(cache.load_intervals({chosen}))
    details = selection
    cpu_started = process_time()
    started = perf_counter()
    if args.workload == "read":
        with module.UsageCache(database, home) as cache:
            event_count, total = 0, 0
            for record, fingerprint in cache.load_events(active):
                event_count += 1
                total += record["total_tokens"]
            interval_count = sum(1 for interval in cache.load_intervals(active))
        details = {"events": event_count, "intervals": interval_count, "total_tokens": total}
    elif args.workload in {"incremental", "report"}:
        if args.workload == "report":
            charts._load_plotting()
            charts.configure_matplotlib()
            from codex_usage.animations import AnimationOptions
            observed_at = datetime.fromisoformat((args.work / "report-observed-at.txt").read_text(encoding="utf-8").strip())
            class FixedReportClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return (observed_at.astimezone(tz) if tz is not None
                            else observed_at.astimezone().replace(tzinfo=None))
            cpu_started = process_time()
            started = perf_counter()
        events, intervals, stats = frozen_pipeline(module, database, home)
        details = {"events": len(events), "intervals": len(intervals), "bytes_read": stats.scan.bytes_read}
        if args.workload == "report":
            output = args.work / f"report-{args.label}"
            output.mkdir(exist_ok=True)
            start, end = events["timestamp"].min().date(), events["timestamp"].max().date()
            with patch.object(charts, "datetime", FixedReportClock):
                summary = charts.export_report(events, stats, output, timezone.utc, "png", 160, 15,
                                              running_intervals=intervals, start=start, end=end,
                                              animation=AnimationOptions(charts=()), progress=False)
            details["charts"] = len(summary["charts"])
            details["total_tokens"] = summary["total_tokens"]
            details["total_running_hours"] = summary["running_time"]["total_running_hours"]
            details["total_summed_running_hours"] = summary["running_time"]["total_summed_running_hours"]
    else:
        with module.UsageCache(database, home) as cache:
            details["transaction_open_seconds"] = perf_counter() - started
            io_before = process_io()
            mutation_started = perf_counter()
            if args.workload == "append":
                stage = perf_counter()
                cache.save_events(chosen, events)
                details["serialize_insert_seconds"] = perf_counter() - stage
                details["appended_events"] = len(events)
            elif args.workload == "delete":
                cache.remove(chosen)
            elif args.workload == "archive":
                cache.move(chosen, f"archived_sessions/benchmark-{Path(chosen).name}")
            elif args.workload == "rebuild":
                cache.remove(chosen)
                cache.save_file(chosen, entries[chosen])
                cache.save_events(chosen, events)
                cache.save_intervals(chosen, intervals)
                details["rebuilt_events"] = len(events)
            mutation_finished = perf_counter()
            details["mutation_seconds"] = mutation_finished - mutation_started
            io_after_mutation = process_io()
        details["commit_close_seconds"] = perf_counter() - mutation_finished
        io_after_commit = process_io()
        if io_before is not None:
            for phase, first, second in (
                ("mutation", io_before, io_after_mutation),
                ("commit", io_after_mutation, io_after_commit),
            ):
                for field in ("write_calls", "write_bytes"):
                    details[f"{phase}_{field}"] = second[field] - first[field]
    seconds = perf_counter() - started
    cpu_seconds = process_time() - cpu_started
    result = {"label": args.label, "workload": args.workload, "seconds": seconds,
              "process_cpu_seconds": cpu_seconds,
              "process_peak_working_set_bytes": peak_bytes(), "preparation": preparation, **details}
    print(json.dumps(result), flush=True)


def run(args):
    PAGE_CACHE_KIB = module_for("v4", args.work).PAGE_CACHE_KIB
    observed_at = None
    if "report" in args.workloads:
        clock_file = args.work / "report-observed-at.txt"
        if not clock_file.exists():
            clock_file.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
        observed_at = datetime.fromisoformat(clock_file.read_text(encoding="utf-8").strip())
        if observed_at.tzinfo is None:
            raise ValueError("Report clock must include an explicit UTC offset")
    sources = source_hashes(args.work)
    if not (args.work / "v4.sqlite3").exists():
        migrate(args)
    equivalence = inspect_equivalence(args.work)
    rows = []
    result_path = args.work / args.results_name
    for workload in args.workloads:
        for iteration in range(args.rounds):
            shift = iteration % len(args.labels)
            labels = args.labels[shift:] + args.labels[:shift]
            for label in labels:
                command = [sys.executable, str(Path(__file__).resolve()), "worker", "--work", str(args.work),
                           "--label", label, "--workload", workload]
                completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True)
                if completed.returncode:
                    raise RuntimeError(f"{workload}/{label} failed: {completed.stderr[-3000:]}")
                row = json.loads(completed.stdout.strip().splitlines()[-1])
                row["iteration"] = iteration + 1
                rows.append(row)
                result_path.write_text(json.dumps({"equivalence": equivalence, "samples": rows},
                                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(row), flush=True)
    summary = {}
    for workload in args.workloads:
        summary[workload] = {}
        for label in args.labels:
            samples = [row for row in rows if row["workload"] == workload and row["label"] == label]
            values = [row["seconds"] for row in samples]
            summary[workload][label] = {
                "seconds": values, "median": statistics.median(values), "worst": max(values),
                "process_cpu_seconds": [row["process_cpu_seconds"] for row in samples],
                "peak_working_set_bytes": max(row["process_peak_working_set_bytes"] for row in samples),
            }
    result = {"run_at": datetime.now(timezone.utc).isoformat(), "python": sys.executable,
              "python_version": sys.version, "sources_sha256": sources,
              "candidate_page_cache_kib": PAGE_CACHE_KIB,
              "report_observed_at": observed_at.isoformat() if observed_at is not None else None,
              "page_cache": "not flushed; alternating warm runs", "equivalence": equivalence,
              "preparation": "closed verified masters; independent byte copies for each mutation; reused working copies for read/incremental/report",
              "summary": summary, "samples": rows}
    if (args.work / "migration.json").exists():
        result["migration"] = json.loads((args.work / "migration.json").read_text(encoding="utf-8"))
    if source_hashes(args.work) != sources:
        raise RuntimeError("Source files changed during this benchmark; completed samples retained as diagnostics")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"results": str(result_path), "equivalence": equivalence, "summary": summary}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "migrate", "verify", "verify-exports", "run", "worker"))
    parser.add_argument("--work", type=Path, default=REPOSITORY / ".task-tmp/runtime-cache-benchmark")
    parser.add_argument("--source", type=Path, help="v3 SQLite cache to copy (required for prepare)")
    parser.add_argument("--revision", default="edda407")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--results-name", default="results.json")
    labels = ("v3", "v3_vacuum", "v4", "v4_8192", "v4_16384")
    parser.add_argument("--labels", nargs="+", choices=labels, default=["v3", "v4"])
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=WORKLOADS)
    parser.add_argument("--label", choices=labels)
    parser.add_argument("--workload", choices=WORKLOADS)
    args = parser.parse_args()
    if args.command == "prepare" and args.source is None:
        parser.error("prepare requires --source")
    args.work = args.work.resolve()
    if not args.work.is_relative_to(REPOSITORY / ".task-tmp"):
        parser.error("--work must stay beneath the repository .task-tmp directory")
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    if Path(args.results_name).name != args.results_name:
        parser.error("--results-name must be a filename")
    {"prepare": prepare, "migrate": migrate, "verify": verify, "verify-exports": verify_exports,
     "run": run, "worker": worker}[args.command](args)


if __name__ == "__main__":
    main()
