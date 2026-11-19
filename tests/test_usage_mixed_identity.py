"""Mixed legacy and response-ID fingerprints deduplicate only with evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from codex_usage.cache import UsageCache
from codex_usage.charts import collect_usage_data


UTC = ZoneInfo("UTC")


def event(kind, second, **payload):
    return {
        "type": kind,
        "timestamp": f"2026-11-17T10:54:{second:02d}Z",
        "payload": payload,
    }


def usage(total=100):
    return {
        "input_tokens": total - 10,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
    }


def token_count(second, values=None, cumulative=None):
    info = {}
    if values is not None:
        info["last_token_usage"] = values
    if cumulative is not None:
        info["total_token_usage"] = cumulative
    return event("event_msg", second, type="token_count", info=info)


def response(second, identity, values=None, cumulative=None):
    return event(
        "token_usage_record",
        second,
        turn_id="turn",
        response_id=identity,
        usage=values,
        thread_token_usage=cumulative,
    )


def rollout(session_id, *records):
    return [
        event("session_meta", 0, id=session_id, cwd="D:/synthetic"),
        event("turn_context", 0, turn_id="turn", model="gpt-6-luna"),
        *records,
    ]


class MixedIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def workspace(self, name):
        base = self.root / name
        home = base / "codex"
        (home / "sessions").mkdir(parents=True)
        return home, base / "cache.sqlite3"

    def write(self, home, name, records):
        path = home / "sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
        )
        return path

    def append(self, path, records):
        with path.open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in records)

    def read_cached(self, home, cache, incremental):
        return collect_usage_data(
            home, UTC, progress=False, cache_path=cache, incremental=incremental
        )

    def read_full(self, home):
        return collect_usage_data(home, UTC, progress=False)

    def test_mixed_exact_copy_deduplicates_in_both_filename_orders(self):
        values = usage()
        for case, modern_name in (("modern-first", "a.jsonl"), ("legacy-first", "z.jsonl")):
            with self.subTest(modern_file=modern_name):
                home, cache = self.workspace(case)
                legacy_name = "z.jsonl" if modern_name == "a.jsonl" else "a.jsonl"
                self.write(
                    home,
                    modern_name,
                    rollout(
                        "a-session" if modern_name == "a.jsonl" else "z-session",
                        response(1, "response-a", values, values),
                        token_count(1, values, values),
                    ),
                )
                self.write(
                    home,
                    legacy_name,
                    rollout(
                        "a-session" if legacy_name == "a.jsonl" else "z-session",
                        token_count(1, values, values),
                    ),
                )

                for mode, result in (
                    ("full", self.read_full(home)),
                    ("cache-rebuild", self.read_cached(home, cache, incremental=False)),
                    ("cache-reuse", self.read_cached(home, cache, incremental=True)),
                ):
                    with self.subTest(mode=mode):
                        frame, _, stats = result
                        self.assertEqual(frame["total_tokens"].tolist(), [100])
                        self.assertEqual(frame["session_id"].tolist(), ["a-session"])
                        self.assertEqual(Path(frame["log_file"].iloc[0]).as_posix(), "sessions/a.jsonl")
                        self.assertEqual(stats.duplicate_events_removed, 1)

    def test_incremental_cache_reconciles_a_copy_added_after_initial_scan(self):
        values = usage()
        home, cache = self.workspace("late-copy")
        self.write(
            home,
            "z-response.jsonl",
            rollout("z-session", response(1, "response-a", values, values), token_count(1, values, values)),
        )

        initial = self.read_cached(home, cache, incremental=True)
        self.assertEqual(initial[0]["total_tokens"].tolist(), [100])

        self.write(
            home,
            "a-legacy.jsonl",
            rollout("a-session", token_count(1, values, values)),
        )
        cached, _, stats = self.read_cached(home, cache, incremental=True)
        fresh = self.read_full(home)

        self.assertEqual(cached["total_tokens"].tolist(), [100])
        self.assertEqual(
            Path(cached["log_file"].iloc[0]).as_posix(), "sessions/a-legacy.jsonl"
        )
        self.assertEqual(cached.to_dict("records"), fresh[0].to_dict("records"))
        self.assertEqual(stats.scan.files_new, 1)
        self.assertEqual(stats.duplicate_events_removed, 1)

    def test_incremental_cache_reconciles_an_appended_mixed_copy(self):
        values = usage()
        home, cache = self.workspace("late-append")
        self.write(
            home,
            "a-legacy.jsonl",
            rollout("a-session", token_count(1, values, values)),
        )
        appended_path = self.write(
            home,
            "b-response.jsonl",
            rollout("b-session"),
        )

        initial = self.read_cached(home, cache, incremental=True)
        self.assertEqual(initial[0]["total_tokens"].tolist(), [100])

        self.append(
            appended_path,
            [response(1, "response-a", values, values), token_count(1, values, values)],
        )
        cached, _, stats = self.read_cached(home, cache, incremental=True)
        fresh = self.read_full(home)

        self.assertEqual(cached["total_tokens"].tolist(), [100])
        self.assertEqual(
            Path(cached["log_file"].iloc[0]).as_posix(), "sessions/a-legacy.jsonl"
        )
        self.assertEqual(cached.to_dict("records"), fresh[0].to_dict("records"))
        self.assertEqual(stats.scan.files_appended, 1)
        self.assertEqual(stats.duplicate_events_removed, 1)

    def test_late_id_bridge_merges_legacy_and_id_copies_transitively(self):
        values = usage()
        home, cache = self.workspace("transitive-bridge")
        self.write(home, "a-legacy.jsonl", rollout("a-session", token_count(1, values, values)))
        self.write(
            home,
            "b-id-t2.jsonl",
            rollout("b-session", response(2, "response-a", values, values), token_count(2, values, values)),
        )
        self.write(
            home,
            "c-id-t1-bridge.jsonl",
            rollout("c-session", response(1, "response-a", values, values), token_count(1, values, values)),
        )

        frame, _, stats = self.read_cached(home, cache, incremental=False)

        self.assertEqual(frame["total_tokens"].tolist(), [100])
        self.assertEqual(frame["timestamp"].dt.second.tolist(), [1])
        self.assertEqual(frame["session_id"].tolist(), ["a-session"])
        self.assertEqual(
            Path(frame["log_file"].iloc[0]).as_posix(), "sessions/a-legacy.jsonl"
        )
        self.assertEqual(stats.duplicate_events_removed, 2)

    def test_equal_sized_no_id_events_without_a_bridge_remain_separate(self):
        values = usage()
        home, _ = self.workspace("no-bridge")
        self.write(home, "a.jsonl", rollout("a-session", token_count(1, values, values)))
        self.write(home, "b.jsonl", rollout("b-session", token_count(2, values, values)))

        frame, _, _ = self.read_full(home)

        self.assertEqual(frame["total_tokens"].tolist(), [100, 100])
        self.assertEqual(frame["timestamp"].dt.second.tolist(), [1, 2])

    def test_incremental_cache_reconciles_a_late_bridge(self):
        values = usage()
        home, cache = self.workspace("late-bridge")
        self.write(home, "a-legacy.jsonl", rollout("a-session", token_count(1, values, values)))
        self.write(home, "b-id-t2.jsonl", rollout(
            "b-session", response(2, "response-a", values, values), token_count(2, values, values)))
        initial = self.read_cached(home, cache, incremental=True)
        self.assertEqual(initial[0]["total_tokens"].tolist(), [100, 100])
        self.write(home, "c-bridge.jsonl", rollout(
            "c-session", response(1, "response-a", values, values), token_count(1, values, values)))
        cached = self.read_cached(home, cache, incremental=True)
        fresh = self.read_full(home)
        self.assertEqual(cached[0]["total_tokens"].tolist(), [100])
        self.assertEqual(cached[0]["session_id"].tolist(), ["a-session"])
        self.assertEqual(cached[0].to_dict("records"), fresh[0].to_dict("records"))
        self.assertEqual(cached[2].duplicate_events_removed, 2)

    def test_earliest_response_time_and_metadata_survive_read_order_and_cache(self):
        values = usage()
        home, cache = self.workspace("cross-hour")
        late = rollout("late-session", response(1, "response-a", values, values),
                       token_count(1, values, values))
        early = rollout("early-session", response(2, "response-a", values, values),
                        token_count(2, values, values))
        for row in late:
            row['timestamp'] = '2026-11-17T12:54:01Z'
        for row in early:
            row['timestamp'] = '2026-11-17T11:54:02Z'
        late[1]['payload']['model'] = 'gpt-6.1-terra'
        self.write(home, 'a-late.jsonl', late)
        self.write(home, 'z-early.jsonl', early)
        expected = self.read_full(home)[0]
        self.assertEqual(expected['session_id'].tolist(), ['early-session'])
        self.assertEqual(expected['model'].tolist(), ['gpt-6-luna'])
        self.assertEqual(expected['timestamp'].dt.hour.tolist(), [11])
        files = sorted((home/'sessions').glob('*.jsonl'), reverse=True)
        with patch('codex_usage.charts.discover_rollouts', return_value=files):
            reversed_full = self.read_full(home)[0]
        pd.testing.assert_frame_equal(expected, reversed_full)
        cached = self.read_cached(home, cache, incremental=False)[0]
        original = UsageCache.load_events

        def reverse_events(instance, active):
            yield from reversed(list(original(instance, active)))

        with patch.object(UsageCache, 'load_events', reverse_events):
            reversed_cache = self.read_cached(home, cache, incremental=True)[0]
        for actual in (cached, reversed_cache):
            pd.testing.assert_frame_equal(expected, actual)
            self.assertEqual(actual.groupby(actual['timestamp'].dt.floor('h'))['total_tokens'].sum().to_dict(),
                             expected.groupby(expected['timestamp'].dt.floor('h'))['total_tokens'].sum().to_dict())

    def test_late_cross_hour_bridge_keeps_earliest_metadata_in_all_read_modes(self):
        values = usage()
        home, cache = self.workspace("cross-hour-bridge")
        early = rollout('early-session', token_count(1, values, values))
        later = rollout('later-session', response(2, 'response-a', values, values),
                        token_count(2, values, values))
        bridge = rollout('bridge-session', response(1, 'response-a', values, values),
                         token_count(1, values, values))
        for row in early + bridge:
            row['timestamp'] = '2026-11-17T11:54:01Z'
        for row in later:
            row['timestamp'] = '2026-11-17T12:54:02Z'
        later[1]['payload']['model'] = 'gpt-6.1-terra'
        self.write(home, 'a-early.jsonl', early)
        self.write(home, 'b-later.jsonl', later)
        initial = self.read_cached(home, cache, incremental=True)[0]
        self.assertEqual(initial['timestamp'].dt.hour.tolist(), [11, 12])
        self.write(home, 'c-bridge.jsonl', bridge)
        expected = self.read_full(home)[0]
        self.assertEqual(expected['session_id'].tolist(), ['early-session'])
        self.assertEqual(expected['model'].tolist(), ['gpt-6-luna'])
        self.assertEqual(expected['timestamp'].dt.hour.tolist(), [11])
        files = sorted((home/'sessions').glob('*.jsonl'), reverse=True)
        with patch('codex_usage.charts.discover_rollouts', return_value=files):
            reversed_full = self.read_full(home)[0]
        cached = self.read_cached(home, cache, incremental=True)[0]
        original = UsageCache.load_events

        def reverse_events(instance, active):
            yield from reversed(list(original(instance, active)))

        with patch.object(UsageCache, 'load_events', reverse_events):
            reversed_cache = self.read_cached(home, cache, incremental=True)[0]
        for actual in (reversed_full, cached, reversed_cache):
            pd.testing.assert_frame_equal(expected, actual)
            self.assertEqual(actual.groupby(actual['timestamp'].dt.floor('h'))['total_tokens'].sum().to_dict(),
                             expected.groupby(expected['timestamp'].dt.floor('h'))['total_tokens'].sum().to_dict())


if __name__ == "__main__":
    unittest.main()
