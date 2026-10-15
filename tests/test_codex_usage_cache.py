"""Incremental reads must agree with a fresh scan of synthetic metadata logs."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from compression import zstd
except ImportError:
    from backports import zstd

from codex_usage.running_time import aggregate_daily_running_time
from codex_usage.cache import CACHE_VERSION
from codex_usage.charts import (
    RAW_TOKEN_COLUMNS,
    collect_usage_data,
    filter_date_range,
    parse_rollout,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def event(kind, timestamp="2026-08-29T01:00:00Z", **payload):
    return {"type": kind, "timestamp": timestamp, "payload": payload}


def tokens(timestamp="2026-08-29T01:02:00Z", total=110, cumulative=False):
    values = {"input_tokens": total - 10, "cached_input_tokens": 20,
              "output_tokens": 10, "total_tokens": total}
    key = "total_token_usage" if cumulative else "last_token_usage"
    return event("event_msg", timestamp, type="token_count", info={key: values})


def rollout(session="main", turn="turn-1", parent=""):
    return [
        event("session_meta", id=session, parent_thread_id=parent, originator="Codex Desktop"),
        event("turn_context", turn_id=turn, model="gpt-6.1-sol"),
        event("event_msg", type="task_started", turn_id=turn),
        tokens(cumulative=True),
    ]


def encode(items):
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items).encode("utf-8")


def compress(raw):
    return zstd.compress(raw, options={zstd.CompressionParameter.checksum_flag: 1})


class IncrementalUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"
        self.sessions = self.home / "sessions"
        self.archived = self.home / "archived_sessions"
        self.sessions.mkdir(parents=True)
        self.archived.mkdir()
        self.cache = self.root / "usage.sqlite3"

    def write(self, name="one.jsonl", items=None):
        path = self.sessions / name
        path.write_bytes(encode(rollout() if items is None else items))
        return path

    def write_compressed(self, name="one.jsonl.zst", items=None, archived=False):
        path = (self.archived if archived else self.sessions) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = encode(rollout() if items is None else items)
        path.write_bytes(compress(raw))
        return path

    def cache_snapshot(self):
        with closing(sqlite3.connect(self.cache)) as connection:
            return {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("metadata", "logs", "events", "intervals")
            }

    def read(self, incremental=True, include_archived=True, target_tz=SHANGHAI):
        return collect_usage_data(
            self.home, target_tz, include_archived, False,
            cache_path=self.cache, incremental=incremental,
        )

    def assert_fresh(self, result, include_archived=True, target_tz=SHANGHAI):
        fresh = collect_usage_data(self.home, target_tz, include_archived, False)
        pd.testing.assert_frame_equal(result[0], fresh[0])
        self.assertEqual(result[1], fresh[1])
        actual_stats = asdict(result[2])
        expected_stats = asdict(fresh[2])
        actual_stats.pop("scan")
        expected_stats.pop("scan")
        self.assertEqual(actual_stats, expected_stats)

    def test_initial_scan_then_unchanged_logs_are_not_parsed(self):
        path = self.write()
        first = self.read()
        self.assert_fresh(first)
        self.assertEqual(first[2].scan.files_new, 1)
        self.assertEqual(first[2].scan.bytes_read, path.stat().st_size)
        with patch("codex_usage.charts.parse_rollout", wraps=parse_rollout) as parser:
            second = self.read()
            parser.assert_not_called()
        self.assert_fresh(second)
        self.assertEqual(second[2].scan.files_reused, 1)
        self.assertEqual(second[2].scan.bytes_read, 0)

    def test_compressed_matches_plain_usage_models_runtime_and_timezone(self):
        items = [
            event("session_meta", id="main", originator="Codex Desktop"),
            event("turn_context", turn_id="turn-1", model="gpt-6.1-sol"),
            event("event_msg", "2026-08-29T15:59:00Z", type="task_started", turn_id="turn-1"),
            tokens("2026-08-29T16:00:00Z", total=110, cumulative=True),
            event("turn_context", "2026-08-29T16:01:00Z", turn_id="turn-1", model="gpt-6-astra"),
            tokens("2026-08-29T16:02:00Z", total=180, cumulative=True),
            event("event_msg", "2026-08-29T16:03:00Z", type="task_complete", turn_id="turn-1"),
        ]
        plain = self.write(items=items)
        zones = (SHANGHAI, ZoneInfo("America/New_York"))
        expected = {
            zone.key: collect_usage_data(self.home, zone, progress=False) for zone in zones
        }
        plain.unlink()
        self.write_compressed(items=items)
        for zone in zones:
            with self.subTest(timezone=zone.key):
                result = self.read(target_tz=zone)
                self.assert_fresh(result, target_tz=zone)
                baseline = expected[zone.key]
                pd.testing.assert_frame_equal(
                    result[0].drop(columns="log_file"), baseline[0].drop(columns="log_file")
                )
                self.assertEqual(
                    [{key: value for key, value in asdict(item).items() if key != "log_file"}
                     for item in result[1]],
                    [{key: value for key, value in asdict(item).items() if key != "log_file"}
                     for item in baseline[1]],
                )
                self.assertEqual(result[0]["total_tokens"].tolist(), [110, 70])
                self.assertEqual(result[0]["model"].tolist(), ["gpt-6.1-sol", "gpt-6-astra"])
                daily = aggregate_daily_running_time(result[1], zone, date(2026, 8, 29), date(2026, 8, 30))
                plain_daily = aggregate_daily_running_time(baseline[1], zone, date(2026, 8, 29), date(2026, 8, 30))
                pd.testing.assert_frame_equal(daily, plain_daily)
                self.assertEqual(
                    daily["running_seconds"].tolist(),
                    [60.0, 180.0] if zone is SHANGHAI else [240.0, 0.0],
                )
                self.assertEqual(
                    result[0]["timestamp"].dt.date.tolist(),
                    [date(2026, 8, 30)] * 2 if zone is SHANGHAI else [date(2026, 8, 29)] * 2,
                )

    def test_compressed_discovery_respects_archive_switch_and_nested_directories(self):
        self.write()
        self.write_compressed("2026/08/29/older.jsonl.zst", rollout("older", "older-turn"), archived=True)
        excluded = self.read(include_archived=False)
        self.assert_fresh(excluded, include_archived=False)
        self.assertEqual(excluded[0]["session_id"].tolist(), ["main"])
        included = self.read()
        self.assert_fresh(included)
        self.assertEqual(set(included[0]["session_id"]), {"main", "older"})
        self.assertEqual(included[2].files_seen, 2)
        self.assertEqual(included[2].scan.files_new, 1)
        excluded_again = self.read(include_archived=False)
        self.assert_fresh(excluded_again, include_archived=False)
        self.assertEqual(excluded_again[2].scan.bytes_read, 0)
        included_again = self.read()
        self.assert_fresh(included_again)
        self.assertEqual(included_again[2].scan.files_reused, 2)
        self.assertEqual(included_again[2].scan.bytes_read, 0)

    def test_compressed_and_plain_copies_deduplicate_tokens_and_running_intervals(self):
        items = rollout() + [event(
            "event_msg", "2026-08-29T01:04:00Z", type="task_complete", turn_id="turn-1"
        )]
        self.write(items=items)
        self.write_compressed("copy.jsonl.zst", items, archived=True)
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[0]["total_tokens"].tolist(), [110])
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(result[2].duplicate_events_removed, 1)
        self.assertEqual(result[2].runtime.duplicate_intervals_removed, 1)

    def test_plain_log_replaced_by_compressed_log_preserves_usage_without_duplicates(self):
        path = self.write()
        before = self.read()
        compressed = self.write_compressed()
        path.unlink()
        result = self.read()
        self.assert_fresh(result)
        pd.testing.assert_frame_equal(
            result[0].drop(columns="log_file"), before[0].drop(columns="log_file")
        )
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(result[2].scan.files_removed, 1)
        self.assertEqual(result[2].scan.files_new, 1)
        self.assertEqual(result[2].scan.bytes_read, compressed.stat().st_size)
        self.assertEqual(result[2].duplicate_events_removed, 0)

    def test_unchanged_compressed_log_reuses_physical_size_checkpoint_without_parsing(self):
        path = self.write_compressed()
        first = self.read()
        self.assert_fresh(first)
        size = path.stat().st_size
        self.assertLess(size, len(encode(rollout())))
        self.assertEqual(first[2].scan.bytes_read, size)
        with closing(sqlite3.connect(self.cache)) as connection:
            offset, state = connection.execute("SELECT offset, state FROM logs").fetchone()
        self.assertEqual(offset, size)
        self.assertEqual(json.loads(state)["offset"], size)
        with patch("codex_usage.charts.parse_rollout", wraps=parse_rollout) as parser:
            second = self.read()
            parser.assert_not_called()
        self.assert_fresh(second)
        self.assertEqual(second[2].scan.files_reused, 1)
        self.assertEqual(second[2].scan.bytes_read, 0)

    def test_replaced_compressed_log_rebuilds_instead_of_resuming_compressed_offset(self):
        path = self.write_compressed()
        self.read()
        replacement = self.sessions / "replacement.tmp"
        items = rollout() + [
            tokens("2026-08-29T01:03:00Z", total=180, cumulative=True),
            event("event_msg", "2026-08-29T01:04:00Z", type="task_complete", turn_id="turn-1"),
        ]
        replacement.write_bytes(compress(encode(items)))
        replacement.replace(path)
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[0]["total_tokens"].tolist(), [110, 70])
        self.assertEqual(result[1][0].status, "completed")
        self.assertEqual(result[2].scan.files_rebuilt, 1)
        self.assertEqual(result[2].scan.files_appended, 0)
        self.assertEqual(result[2].scan.bytes_read, path.stat().st_size)

    def test_appended_zstd_frame_rebuilds_and_keeps_cumulative_usage_and_open_turn(self):
        path = self.write_compressed()
        first = self.read()
        self.assertEqual(first[1][0].status, "incomplete")
        extra = [
            tokens("2026-08-30T01:03:00Z", total=180, cumulative=True),
            event("event_msg", "2026-08-30T01:04:00Z", type="task_complete", turn_id="turn-1"),
        ]
        with path.open("ab") as handle:
            handle.write(compress(encode(extra)))
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[0]["total_tokens"].tolist(), [110, 70])
        self.assertEqual(result[0]["model"].tolist(), ["gpt-6.1-sol"] * 2)
        self.assertEqual(result[0]["turn_id"].tolist(), ["turn-1"] * 2)
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(result[1][0].status, "completed")
        self.assertEqual(result[2].scan.files_rebuilt, 1)
        self.assertEqual(result[2].scan.files_appended, 0)
        self.assertEqual(result[2].scan.bytes_read, path.stat().st_size)
        self.assertEqual(self.read()[2].scan.bytes_read, 0)

    def test_high_compression_ratio_limits_output_and_preserves_lines_across_frames(self):
        items = rollout() + [event(
            "event_msg", "2026-08-29T01:02:30Z", type="task_progress", padding="x" * 700
        )] * 4096
        last_token = tokens("2026-08-29T01:03:00Z", total=180, cumulative=True)
        last_token["payload"]["ignored_metadata"] = "跨帧"
        items.extend([
            last_token,
            event("event_msg", "2026-08-29T01:04:00Z", type="task_complete", turn_id="turn-1"),
        ])
        # A complete Zstd frame may end in the middle of JSON or even a UTF-8
        # character. The final JSONL record also need not have been completed.
        raw = encode(items) + encode([tokens("2026-08-29T01:05:00Z", total=999)])[:-1]
        plain = self.write()
        plain.write_bytes(raw)
        baseline = collect_usage_data(self.home, SHANGHAI, progress=False)
        plain.unlink()
        split = raw.index("跨".encode("utf-8")) + 1
        compressed = compress(raw[:split]) + compress(raw[split:split + 1]) + compress(raw[split + 1:])
        path = self.sessions / "many-lines.jsonl.zst"
        path.write_bytes(compressed)
        self.assertGreater(len(raw), len(compressed) * 100)

        decoder_type = zstd.ZstdDecompressor
        output_sizes = []
        test_case = self

        class BoundedDecoder:
            def __init__(self):
                self.decoder = decoder_type()

            def decompress(self, data, max_length=-1):
                test_case.assertGreater(max_length, 0, "解压必须限制单次输出长度")
                test_case.assertLessEqual(max_length, 128 * 1024)
                output = self.decoder.decompress(data, max_length=max_length)
                test_case.assertLessEqual(len(output), max_length)
                output_sizes.append(len(output))
                return output

            def __getattr__(self, name):
                return getattr(self.decoder, name)

        with patch.object(zstd, "ZstdDecompressor", BoundedDecoder):
            result = self.read()
        self.assertGreater(len(output_sizes), 10)
        self.assertEqual(sum(output_sizes), len(raw))
        self.assert_fresh(result)
        pd.testing.assert_frame_equal(
            result[0].drop(columns="log_file"), baseline[0].drop(columns="log_file")
        )
        self.assertEqual(result[0]["total_tokens"].tolist(), [110, 70])
        self.assertEqual(result[2].malformed_lines, 0)
        self.assertEqual(result[1][0].status, "completed")

    def test_corrupt_and_truncated_compressed_logs_roll_back_all_cached_files(self):
        plain = self.write("a.jsonl")
        path = self.write_compressed("b.jsonl.zst", rollout("other", "other-turn"))
        valid = path.read_bytes()
        next_frame = compress(encode([
            tokens("2026-08-30T01:03:00Z", total=180, cumulative=True)
        ]))
        self.read()
        broken_variants = {
            "corrupt_header": b"BAD!" + valid[4:],
            "truncated_frame": valid[:-1],
            "truncated_second_frame": valid + next_frame[:-1],
            "corrupt_second_frame": valid + b"BAD!" + next_frame[4:],
        }
        for index, (kind, broken) in enumerate(broken_variants.items()):
            with self.subTest(kind=kind):
                snapshot = self.cache_snapshot()
                with plain.open("ab") as handle:
                    handle.write(encode([tokens(f"2026-08-29T01:{index + 3:02d}:00Z", total=140 + index)]))
                path.write_bytes(broken)
                with self.assertRaisesRegex(OSError, "读取日志失败"):
                    self.read()
                self.assertEqual(self.cache_snapshot(), snapshot)
                path.write_bytes(valid)
                repaired = self.read()
                self.assert_fresh(repaired)
                self.assertEqual(len(repaired[0]), index + 3)

    def test_append_resumes_model_turn_and_total_delta_and_completes_open_turn(self):
        path = self.write()
        first = self.read(incremental=False)
        self.assertEqual(first[1][0].status, "incomplete")
        additional = encode([
            tokens("2026-08-30T01:03:00Z", total=180, cumulative=True),
            event("event_msg", "2026-08-30T01:04:00Z", type="task_complete", turn_id="turn-1"),
        ])
        with path.open("ab") as handle:
            handle.write(additional)
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[0]["total_tokens"].tolist(), [110, 70])
        self.assertEqual(result[0]["model"].tolist(), ["gpt-6.1-sol"] * 2)
        self.assertEqual(result[0]["turn_id"].tolist(), ["turn-1"] * 2)
        self.assertEqual(len(result[1]), 1)
        self.assertEqual(result[1][0].status, "completed")
        self.assertEqual(result[2].scan.files_appended, 1)
        self.assertEqual(result[2].scan.bytes_read, len(additional))
        self.assertEqual(self.read()[2].scan.bytes_read, 0)

    def test_partial_utf8_line_waits_and_is_read_exactly_once_when_completed(self):
        path = self.write()
        first_size = path.stat().st_size
        self.read()
        item = tokens("2026-08-29T01:03:00Z", total=120)
        item["payload"]["ignored_content"] = "隐私内容"
        raw = encode([item])
        split = raw.index("隐".encode("utf-8")) + 1
        with path.open("ab") as handle:
            handle.write(raw[:split])
        partial = self.read()
        self.assert_fresh(partial)
        self.assertEqual(len(partial[0]), 1)
        with closing(sqlite3.connect(self.cache)) as connection:
            offset = connection.execute("SELECT offset FROM logs").fetchone()[0]
        self.assertEqual(offset, first_size)
        with path.open("ab") as handle:
            handle.write(raw[split:])
        complete = self.read()
        self.assert_fresh(complete)
        self.assertEqual(len(complete[0]), 2)
        self.assertEqual(complete[2].malformed_lines, 0)
        self.assertEqual(complete[2].scan.bytes_read, len(raw))

    def test_new_subagents_and_header_bridges_reassign_cached_events_to_root(self):
        self.write("child.jsonl", rollout("child", "child-turn", "bridge"))
        self.read()
        self.write("bridge.jsonl", [event(
            "session_meta", id="bridge",
            source={"subagent": {"thread_spawn": {"parent_thread_id": "main"}}},
        )])
        self.write("main.jsonl", rollout("main", "main-turn"))
        self.write("grandchild.jsonl", rollout("grandchild", "grandchild-turn", "child"))
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[0]["thread_id"].unique().tolist(), ["main"])
        self.assertEqual(len(result[0]), 3)
        self.assertEqual(result[2].scan.files_reused, 1)

    def test_archive_move_preserves_cursor_copy_deduplicates_and_scope_can_change(self):
        path = self.write()
        self.read()
        moved = self.archived / path.name
        path.rename(moved)
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[2].scan.files_moved, 1)
        self.assertEqual(result[2].scan.bytes_read, 0)
        self.write("copy.jsonl", rollout())
        copied = self.read()
        self.assert_fresh(copied)
        self.assertEqual(len(copied[0]), 1)
        self.assertEqual(copied[2].duplicate_events_removed, 1)
        excluded = self.read(include_archived=False)
        self.assert_fresh(excluded, include_archived=False)
        self.assertEqual(excluded[2].scan.bytes_read, 0)
        included = self.read()
        self.assert_fresh(included)
        self.assertEqual(included[2].scan.bytes_read, 0)

    def test_truncate_same_size_edit_and_file_replacement_rebuild_only_that_file(self):
        path = self.write()
        self.write("unchanged.jsonl", rollout("other", "other-turn"))
        self.read()
        short = [event("session_meta", id="main"), tokens(total=80)]
        path.write_bytes(encode(short))
        truncated = self.read()
        self.assert_fresh(truncated)
        self.assertEqual(truncated[2].scan.files_rebuilt, 1)
        self.assertEqual(truncated[2].scan.files_reused, 1)
        original_stat = path.stat()
        short[-1] = tokens(total=90)
        path.write_bytes(encode(short))
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
        edited = self.read()
        self.assert_fresh(edited)
        self.assertEqual(edited[2].scan.files_rebuilt, 1)
        replacement = self.sessions / "replacement.tmp"
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        replacement.replace(path)
        replaced = self.read()
        self.assert_fresh(replaced)
        self.assertEqual(replaced[2].scan.files_rebuilt, 1)

    def test_deleted_logs_are_removed_instead_of_preserving_unavailable_usage(self):
        path = self.write()
        self.write("other.jsonl", rollout("other", "other-turn"))
        self.read()
        path.unlink()
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[2].scan.files_removed, 1)
        self.assertEqual(len(result[0]), 1)

    def test_timezone_date_filter_and_title_changes_need_no_log_reparse(self):
        self.write()
        self.read()
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": "main", "thread_name": "更新标题"}), encoding="utf-8"
        )
        eastern = ZoneInfo("America/New_York")
        result = self.read(target_tz=eastern)
        self.assert_fresh(result, target_tz=eastern)
        self.assertEqual(result[2].scan.bytes_read, 0)
        self.assertEqual(result[0].loc[0, "title"], "更新标题")
        self.assertEqual(result[0].loc[0, "timestamp"].date(), date(2026, 8, 28))
        self.assertTrue(filter_date_range(result[0], date(2026, 8, 29), None).empty)
        self.assertEqual(len(self.read()[0]), 1)

    def test_failed_update_rolls_back_all_files_and_retry_does_not_double_count(self):
        path = self.write("a.jsonl")
        self.read()
        with closing(sqlite3.connect(self.cache)) as connection:
            original_offset = connection.execute("SELECT offset FROM logs").fetchone()[0]
        with path.open("ab") as handle:
            handle.write(encode([tokens("2026-08-29T01:03:00Z", total=140)]))
        self.write("b.jsonl", rollout("other", "other-turn"))

        def interrupted(path, *args, **kwargs):
            if path.name == "b.jsonl":
                raise KeyboardInterrupt("simulate cancelled worker")
            yield from parse_rollout(path, *args, **kwargs)

        with patch("codex_usage.charts.parse_rollout", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.read()
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertEqual(connection.execute("SELECT offset FROM logs").fetchone()[0], original_offset)
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(len(result[0]), 3)

    def test_full_read_ignores_progress_and_refreshes_cache_for_next_increment(self):
        self.write()
        self.read()
        with patch("codex_usage.charts.parse_rollout", wraps=parse_rollout) as parser:
            full = self.read(incremental=False)
            self.assertEqual(parser.call_count, 1)
        self.assert_fresh(full)
        self.assertEqual(full[2].scan.mode, "full")
        self.assertEqual(self.read()[2].scan.bytes_read, 0)

    def test_corrupt_or_old_version_cache_is_preserved_and_rebuilt(self):
        self.write()
        self.cache.write_bytes(b"broken cache")
        repaired = self.read()
        self.assert_fresh(repaired)
        self.assertEqual(len(list(self.root.glob("usage.sqlite3.invalid-*.bak"))), 1)
        with closing(sqlite3.connect(self.cache)) as connection:
            connection.execute(f"PRAGMA user_version = {CACHE_VERSION + 1}")
        rebuilt = self.read()
        self.assert_fresh(rebuilt)
        self.assertEqual(len(list(self.root.glob("usage.sqlite3.invalid-*.bak"))), 2)

    def test_switching_codex_home_does_not_mix_sources(self):
        self.write()
        self.read()
        other_home = self.root / "other-codex"
        (other_home / "sessions").mkdir(parents=True)
        (other_home / "sessions" / "other.jsonl").write_bytes(encode(rollout("different")))
        result = collect_usage_data(
            other_home, SHANGHAI, progress=False, cache_path=self.cache, incremental=True
        )
        self.assertEqual(result[0]["session_id"].unique().tolist(), ["different"])
        self.assertEqual(len(result[0]), 1)

    def test_invalid_checkpoint_and_cached_event_are_rebuilt(self):
        self.write()
        self.read()
        for statement in ("UPDATE logs SET state = '{}'", "UPDATE events SET record = '{}'",
                          "UPDATE logs SET stats = '{\"runtime\":[]}'"):
            with closing(sqlite3.connect(self.cache)) as connection:
                connection.execute(statement)
                connection.commit()
            repaired = self.read()
            self.assert_fresh(repaired)
            self.assertEqual(len(repaired[0]), 1)

    def test_cache_cannot_overwrite_or_be_created_among_original_logs(self):
        path = self.write()
        original = path.read_bytes()
        index = self.home / "session_index.jsonl"
        index.write_text('{"id":"main","thread_name":"标题"}\n', encoding="utf-8")
        original_index = index.read_bytes()
        for invalid in (path, index, self.archived / "new-cache.sqlite3"):
            with self.assertRaisesRegex(ValueError, "原始日志"):
                collect_usage_data(self.home, SHANGHAI, progress=False, cache_path=invalid, incremental=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(index.read_bytes(), original_index)
        self.assertFalse((self.archived / "new-cache.sqlite3").exists())

    def test_cache_has_no_message_reasoning_tool_or_nested_source_bodies(self):
        private = "PRIVATE_MESSAGE_REASONING_TOOL_CONTENT"
        items = rollout()
        items[0]["payload"]["base_instructions"] = private
        items[0]["payload"]["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": "root", "message": private}}
        }
        items.insert(3, event("response_item", content=private))
        items.insert(3, event("event_msg", type="agent_reasoning", text=private))
        items.insert(3, event("event_msg", type="mcp_tool_call_end", output=private))
        self.write(items=items)
        result = self.read()
        self.assert_fresh(result)
        self.assertNotIn(private, self.cache.read_bytes().decode("utf-8", errors="replace"))
        with closing(sqlite3.connect(self.cache)) as connection:
            record = json.loads(connection.execute("SELECT record FROM events").fetchone()[0])
        self.assertNotIn("content", record)
        self.assertEqual(record["total_tokens"], 110)
        self.assertTrue(set(RAW_TOKEN_COLUMNS) - {"cache_write_input_tokens", "reasoning_output_tokens"} <= record.keys())

    def test_empty_logs_and_corrupt_complete_lines_remain_resumable(self):
        empty = self.read()
        self.assert_fresh(empty)
        path = self.write()
        with path.open("ab") as handle:
            handle.write(b'{"bad json":\n')
        result = self.read()
        self.assert_fresh(result)
        self.assertEqual(result[2].malformed_lines, 1)
        with path.open("ab") as handle:
            handle.write(encode([tokens("2026-08-29T01:03:00Z", total=140)]))
        resumed = self.read()
        self.assert_fresh(resumed)
        self.assertEqual(len(resumed[0]), 2)


if __name__ == "__main__":
    unittest.main()
