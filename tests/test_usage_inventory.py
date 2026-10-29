"""Inventory remains complete without retaining conversation bodies."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from codex_usage.cache import CACHE_VERSION
from codex_usage.charts import collect_usage_data


def event(kind, timestamp="2026-10-26T01:00:00Z", **payload):
    return {"type": kind, "timestamp": timestamp, "payload": payload}


def token(timestamp="2026-10-26T01:01:00Z", **payload):
    return event("event_msg", timestamp, type="token_count", info={
        "last_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    }, **payload)


def encode(items):
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items).encode("utf-8")


class UsageInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex"
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True)
        self.cache = self.root / "usage.sqlite3"
        self.tz = ZoneInfo("Asia/Shanghai")

    def write(self, name, items):
        path = self.sessions / name
        path.write_bytes(encode(items))
        return path

    def read(self, *, cached=False):
        inventory = {"sessions": {"stale": {}}, "parents": {"stale": ""}, "turns": ["stale"]}
        result = collect_usage_data(
            self.home, self.tz, progress=False, inventory=inventory,
            cache_path=self.cache if cached else None, incremental=cached,
        )
        return result, inventory

    def test_header_only_bridges_and_turns_without_tokens_are_included(self):
        self.write("a-main.jsonl", [
            event("session_meta", id="main", cwd="D:/project"),
            event("event_msg", type="task_started", turn_id="empty-turn"),
            event("turn_context", turn_id="empty-turn", model="gpt-test", effort="high"),
            event("event_msg", type="task_complete", turn_id="empty-turn"),
        ])
        self.write("b-bridge.jsonl", [event("session_meta", id="bridge", source={
            "subagent": {"thread_spawn": {
                "parent_thread_id": "main", "parent_turn_id": "empty-turn",
            }},
        })])
        self.write("c-leaf.jsonl", [
            event("session_meta", id="leaf", source={"thread_spawn": {
                "parent_thread_id": "bridge", "parent_turn_id": "bridge-turn",
            }}),
            event("turn_context", turn_id="leaf-turn", model="gpt-other", reasoning_effort="low"),
            token(),
        ])

        result, inventory = self.read()

        self.assertEqual(set(inventory["sessions"]), {"main", "bridge", "leaf"})
        self.assertEqual(inventory["parents"], {"main": "", "bridge": "main", "leaf": "bridge"})
        self.assertEqual(inventory["sessions"]["main"]["started_at"], "2026-10-26T01:00:00+00:00")
        self.assertEqual(inventory["sessions"]["bridge"]["parent_turn_id"], "empty-turn")
        self.assertEqual(inventory["sessions"]["leaf"]["parent_turn_id"], "bridge-turn")
        self.assertEqual(inventory["turns"], [
            {"session_id": "leaf", "turn_id": "leaf-turn", "models": ["gpt-other"], "reasoning_efforts": ["low"]},
            {"session_id": "main", "turn_id": "empty-turn", "models": ["gpt-test"], "reasoning_efforts": ["high"]},
        ])
        self.assertEqual(result[0]["thread_id"].tolist(), ["main"])
        self.assertEqual(result[0]["reasoning_effort"].tolist(), ["low"])
        cached_result, cached_inventory = self.read(cached=True)
        self.assertEqual(cached_inventory, inventory)
        pd.testing.assert_frame_equal(cached_result[0], result[0])

    def test_incremental_context_effort_and_turn_inventory_survive_reuse(self):
        path = self.write("one.jsonl", [
            event("session_meta", id="main"),
            event("event_msg", type="task_started", turn_id="first"),
            event("turn_context", turn_id="first", model="gpt-a", effort="high"),
            token(),
        ])
        self.read(cached=True)
        appended = [
            token("2026-10-26T01:02:00Z"),
            event("turn_context", turn_id="first", model="gpt-b", reasoning_effort="low"),
            token("2026-10-26T01:03:00Z"),
            event("event_msg", type="task_complete", turn_id="first"),
            event("event_msg", type="task_started", turn_id="second"),
            event("turn_context", turn_id="second", model="gpt-c", effort={"ignored": "body"}),
            token("2026-10-26T01:04:00Z"),
        ]
        with path.open("ab") as handle:
            handle.write(encode(appended))
        result, inventory = self.read(cached=True)
        fresh_result, fresh_inventory = self.read()

        self.assertEqual(inventory, fresh_inventory)
        pd.testing.assert_frame_equal(result[0], fresh_result[0])
        self.assertEqual(result[2].scan.files_appended, 1)
        self.assertEqual(result[0]["reasoning_effort"].tolist(), ["high", "high", "low", ""])
        self.assertEqual(inventory["turns"], [
            {"session_id": "main", "turn_id": "first", "models": ["gpt-a", "gpt-b"], "reasoning_efforts": ["high", "low"]},
            {"session_id": "main", "turn_id": "second", "models": ["gpt-c"], "reasoning_efforts": []},
        ])
        with patch("codex_usage.charts.parse_rollout", side_effect=AssertionError("unchanged logs were read")):
            reused, reused_inventory = self.read(cached=True)
        self.assertEqual(reused_inventory, inventory)
        self.assertEqual(reused[2].scan.bytes_read, 0)

    def test_missing_metadata_is_not_invented_and_token_turn_ids_are_registered(self):
        self.write("missing.jsonl", [
            token(),
            token("2026-10-26T01:02:00Z", turn_id="token-turn"),
            event("event_msg", type="task_started", turn_id="unmeasured"),
            event("event_msg", type="token_count", info=None),
        ])
        result, inventory = self.read()
        self.assertEqual(inventory["sessions"], {})
        self.assertEqual(inventory["parents"], {})
        self.assertEqual(inventory["turns"], [
            {"session_id": "", "turn_id": "token-turn", "models": [], "reasoning_efforts": []},
            {"session_id": "", "turn_id": "unmeasured", "models": [], "reasoning_efforts": []},
        ])
        self.assertEqual(result[0]["turn_id"].tolist(), ["", "token-turn"])
        self.assertEqual(result[0]["reasoning_effort"].tolist(), ["", ""])
        self.assertEqual(self.read(cached=True)[1], inventory)

    def test_duplicate_logs_merge_inventory_without_changing_token_deduplication(self):
        items = [
            event("session_meta", id="main"),
            event("turn_context", turn_id="turn", model="gpt-a", effort="high"),
            token(),
        ]
        self.write("a.jsonl", items)
        self.write("b.jsonl", items)
        result, inventory = self.read(cached=True)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[2].duplicate_events_removed, 1)
        self.assertEqual(len(inventory["turns"]), 1)
        self.assertEqual(inventory, self.read()[1])

    def test_message_bodies_and_extra_source_context_fields_are_not_persisted(self):
        secret = "DO_NOT_PERSIST_正文_93"
        self.write("private.jsonl", [
            event("session_meta", id="child", instructions=secret, source={
                "unrecognized": secret,
                "subagent": {"thread_spawn": {
                    "parent_thread_id": "main", "parent_turn_id": "parent-turn", "prompt": secret,
                }, "message": secret},
            }),
            event("turn_context", turn_id="turn", model="gpt-a", effort="high", prompt=secret),
            event("event_msg", type="task_started", turn_id="turn", message=secret),
            event("response_item", type="message", content=[{"text": secret}]),
            token(message=secret),
        ])
        result, inventory = self.read(cached=True)
        self.assertNotIn(secret, json.dumps(inventory, ensure_ascii=False))
        self.assertNotIn(secret, result[0].to_json(force_ascii=False))
        with closing(sqlite3.connect(self.cache)) as connection:
            for table, column in (("logs", "state"), ("events", "record"), ("intervals", "record")):
                rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
                self.assertNotIn(secret, str(rows))
        self.assertEqual(inventory["sessions"]["child"]["source"], {
            "subagent": {"thread_spawn": {"parent_thread_id": "main", "parent_turn_id": "parent-turn"}},
        })

    def test_old_cache_rebuilds_inventory_and_removed_logs_are_not_retained(self):
        path = self.write("one.jsonl", [event("session_meta", id="main"),
                                        event("event_msg", type="task_started", turn_id="turn")])
        _, expected = self.read(cached=True)
        with closing(sqlite3.connect(self.cache)) as connection:
            connection.execute(f"PRAGMA user_version = {CACHE_VERSION - 1}")
        _, rebuilt = self.read(cached=True)
        self.assertEqual(rebuilt, expected)
        self.assertEqual(len(list(self.root.glob("usage.sqlite3.invalid-*.bak"))), 1)
        path.unlink()
        result, inventory = self.read(cached=True)
        self.assertTrue(result[0].empty)
        self.assertEqual(inventory, {"sessions": {}, "parents": {}, "turns": []})


if __name__ == "__main__":
    unittest.main()
