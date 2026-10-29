"""External callers query synthetic local logs without rendering or guessing usage."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_usage.query import query_usage


LUNA = "gpt-6-luna"
SOL = "gpt-6.1-sol"


def item(kind, stamp="2026-10-26T00:00:00Z", **payload):
    return {"type": kind, "timestamp": stamp, "payload": payload}


def token(stamp, *, input_tokens=100, output_tokens=20, total_tokens=None,
          cached_input_tokens=0, reasoning_output_tokens=0, total=None):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
             "cached_input_tokens": cached_input_tokens,
             "reasoning_output_tokens": reasoning_output_tokens,
             "total_tokens": total_tokens if total_tokens is not None else input_tokens + output_tokens}
    info = {"last_token_usage": usage}
    if total is not None:
        info["total_token_usage"] = total
    return item("event_msg", stamp, type="token_count", info=info)


def context(turn, model=LUNA, stamp="2026-10-26T00:00:00Z", **payload):
    return item("turn_context", stamp, turn_id=turn, model=model, **payload)


def started(turn, stamp="2026-10-26T00:00:00Z"):
    return item("event_msg", stamp, type="task_started", turn_id=turn)


def completed(turn, stamp="2026-10-26T00:10:00Z"):
    return item("event_msg", stamp, type="task_complete", turn_id=turn)


class UsageQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)

    def write(self, session, records=(), *, parent=None, archived=False, filename=None):
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
        records = [item("session_meta", id=session, source=source, cwd="D:/synthetic"), *records]
        directory = self.home / ("archived_sessions" if archived else "sessions")
        directory.mkdir(exist_ok=True)
        path = directory / (filename or f"{session}.jsonl")
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        return path

    def query(self, **kwargs):
        result = query_usage(self.home, **kwargs)
        # Every public result must serialize using strict JSON without NaN.
        json.dumps(result, allow_nan=False)
        return result

    def tree(self):
        self.write("main", [context("main-turn", SOL), token("2026-10-26T00:01:00Z")])
        self.write("bridge", parent="main")
        self.write("leaf", [context("leaf-turn"), token("2026-10-26T00:02:00Z", input_tokens=200)], parent="bridge")
        self.write("other", [context("other-turn"), token("2026-10-26T00:03:00Z", input_tokens=300)])

    def test_exact_session_descendants_and_root_thread_have_distinct_scopes(self):
        self.tree()
        exact = self.query(session_ids=["main"])
        self.assertEqual(exact["summary"]["total_tokens"], 120)
        descendants = self.query(session_ids=["main"], include_descendants=True)
        self.assertEqual(descendants["summary"]["total_tokens"], 340)
        self.assertEqual(descendants["matches"]["session_ids"], ["bridge", "leaf", "main"])
        self.assertEqual(descendants["diagnostics"]["sessions_without_usage"], ["bridge"])
        task = self.query(thread_ids=["main"])
        self.assertEqual(task["summary"], descendants["summary"])
        leaf = self.query(session_ids=["bridge"], include_descendants=True)
        self.assertEqual(leaf["summary"]["total_tokens"], 220)
        self.assertEqual(leaf["events"][0]["thread_id"], "main")

    def test_lists_use_or_and_dimensions_intersect(self):
        self.tree()
        result = self.query(session_ids=["main", "leaf"], turn_ids=["leaf-turn", "other-turn"], models=[LUNA, "unused"])
        self.assertEqual(result["matches"]["session_ids"], ["leaf"])
        self.assertEqual(result["summary"]["total_tokens"], 220)
        self.assertEqual(result["diagnostics"]["unmatched_ids"]["models"], ["unused"])
        self.assertEqual(result["diagnostics"]["filtered_out_ids"]["turn_ids"], ["other-turn"])
        # A parent turn ID never guesses which child turns it launched.
        parent_turn = self.query(thread_ids=["main"], turn_ids=["main-turn"], models=[LUNA])
        self.assertEqual(parent_turn["summary"]["usage_event_count"], 0)

    def test_header_only_known_sessions_and_unknown_ids_are_distinguished(self):
        self.write("header")
        known = self.query(session_ids=["header"])
        self.assertEqual(known["summary"]["total_tokens"], 0)
        self.assertEqual(known["matches"]["session_ids"], ["header"])
        self.assertEqual(known["diagnostics"]["unmatched_ids"]["session_ids"], [])
        self.assertEqual(known["diagnostics"]["sessions_without_usage"], ["header"])
        unknown = self.query(session_ids=["missing"])
        self.assertEqual(unknown["sessions"], [])
        self.assertEqual(unknown["events"], [])
        self.assertEqual(unknown["diagnostics"]["unmatched_ids"]["session_ids"], ["missing"])
        self.assertTrue(unknown["diagnostics"]["complete"])

    def test_zero_input_is_null_and_token_subsets_are_not_added_twice(self):
        self.write("one", [context("t"), token("2026-10-26T00:01:00Z", input_tokens=0, output_tokens=80, reasoning_output_tokens=60)])
        summary = self.query()["summary"]
        self.assertIsNone(summary["cache_miss_percent"])
        self.assertEqual(summary["total_tokens"], 80)
        self.assertEqual(summary["reasoning_output_tokens"], 60)
        self.assertEqual(summary["visible_output_tokens"], 20)
        self.assertEqual(sum(summary[key] for key in ("uncached_input_tokens", "cached_input_component", "cache_write_input_component", "visible_output_tokens", "reasoning_output_component", "unclassified_tokens")), 80)

    def test_cache_miss_ratio_weights_token_counts(self):
        self.write("one", [context("t"),
                           token("2026-10-26T00:01:00Z", input_tokens=100, cached_input_tokens=0),
                           token("2026-10-26T00:02:00Z", input_tokens=900, cached_input_tokens=900)])
        self.assertAlmostEqual(self.query()["summary"]["cache_miss_percent"], 10)

    def test_last_usage_wins_and_identical_copies_are_deduplicated(self):
        records = [context("same-turn"), token("2026-10-26T00:01:00Z", total={"input_tokens": 99999, "output_tokens": 9999})]
        self.write("one", records, filename="a.jsonl")
        self.write("one", records, filename="b.jsonl")
        result = self.query(session_ids=["one"])
        self.assertEqual(result["summary"]["usage_event_count"], 1)
        self.assertEqual(result["summary"]["total_tokens"], 120)
        self.assertEqual(result["diagnostics"]["parse_stats"]["duplicate_events_removed"], 1)

    def test_fork_copies_keep_canonical_usage_and_runtime_owners_without_double_counting(self):
        shared = [started("shared-turn"), context("shared-turn"), token("2026-10-26T00:01:00Z")]
        self.write("original", shared, filename="a-original.jsonl")
        self.write("fork", [*shared, completed("shared-turn")], filename="b-fork.jsonl")
        expected_ambiguity = [{
            "turn_id": "shared-turn", "observed_session_ids": ["fork", "original"],
            "usage_owner_session_ids": ["original"], "runtime_owner_session_ids": ["fork"],
        }]

        original = self.query(session_ids=["original"])
        self.assertEqual(original["summary"]["total_tokens"], 120)
        self.assertEqual(original["summary"]["running_seconds"], 0)
        self.assertEqual(original["diagnostics"]["ownership_ambiguities"], expected_ambiguity)
        fork = self.query(session_ids=["fork"])
        self.assertEqual(fork["summary"]["total_tokens"], 0)
        self.assertEqual(fork["summary"]["running_seconds"], 600)
        self.assertEqual(fork["diagnostics"]["ownership_ambiguities"], expected_ambiguity)
        turn = self.query(turn_ids=["shared-turn"])
        self.assertEqual(turn["summary"]["total_tokens"], 120)
        self.assertEqual(turn["summary"]["usage_event_count"], 1)
        self.assertEqual(turn["summary"]["running_seconds"], 600)
        self.assertEqual(turn["summary"]["summed_running_seconds"], 600)
        self.assertEqual(turn["diagnostics"]["ownership_ambiguities"], expected_ambiguity)

    def test_precise_window_is_half_open_and_clips_overlapping_intervals(self):
        self.write("a", [started("a-turn"), context("a-turn"),
                         token("2026-10-26T00:02:00Z"), token("2026-10-26T00:03:00Z"),
                         token("2026-10-26T00:07:00Z"), completed("a-turn")])
        self.write("b", [started("b-turn", "2026-10-26T00:05:00Z"),
                         context("b-turn", stamp="2026-10-26T00:05:00Z"),
                         token("2026-10-26T00:06:00Z"), completed("b-turn", "2026-10-26T00:15:00Z")])
        result = self.query(start="2026-10-26T08:03:00", end="2026-10-26T08:07:00", timezone="Asia/Shanghai")
        self.assertEqual(result["summary"]["usage_event_count"], 2)
        self.assertEqual(result["summary"]["running_seconds"], 240)
        self.assertEqual(result["summary"]["summed_running_seconds"], 360)
        self.assertEqual([row["duration_seconds"] for row in result["running_intervals"]], [240, 120])
        self.assertTrue(all(row["timestamp"].endswith("+08:00") for row in result["events"]))
        self.assertEqual(result["running_intervals"][0]["observed_start"], "2026-10-26T08:00:00+08:00")

    def test_unfinished_turn_stops_at_last_activity_and_keeps_incomplete_status(self):
        self.write("one", [started("t"), context("t"), token("2026-10-26T00:02:00Z"),
                           item("session_meta", "2026-10-26T12:00:00Z", id="one")])
        result = self.query()
        self.assertEqual(result["summary"]["running_seconds"], 120)
        self.assertEqual(result["summary"]["incomplete_interval_count"], 1)
        self.assertEqual(result["diagnostics"]["incomplete_turns"], [{"session_id": "one", "turn_id": "t", "runtime_included": True}])
        self.assertEqual(result["running_intervals"][0]["end"], "2026-10-26T00:02:00+00:00")

    def test_model_filter_does_not_allocate_mixed_or_unknown_turn_runtime(self):
        self.write("mixed", [started("mixed-turn"), context("mixed-turn"), token("2026-10-26T00:01:00Z"),
                             context("mixed-turn", SOL, "2026-10-26T00:02:00Z"),
                             token("2026-10-26T00:03:00Z"), completed("mixed-turn")])
        self.write("unknown", [started("unknown-turn"), completed("unknown-turn")])
        self.write("known", [started("known-turn"), context("known-turn"), token("2026-10-26T00:04:00Z"), completed("known-turn")])
        result = self.query(models=[LUNA])
        self.assertEqual(result["summary"]["usage_event_count"], 2)
        self.assertEqual(result["summary"]["summed_running_seconds"], 600)
        self.assertEqual({row["session_id"] for row in result["running_intervals"]}, {"known"})
        self.assertEqual({row["reason"] for row in result["diagnostics"]["excluded_runtime"]}, {"mixed_turn_models", "unknown_turn_model"})
        mixed = self.query(session_ids=["mixed"], models=[LUNA, SOL])
        self.assertEqual(mixed["summary"]["summed_running_seconds"], 600)

    def test_runtime_only_turn_is_a_known_invocation_with_missing_usage(self):
        self.write("one", [started("t"), context("t", effort="max"), completed("t")])
        result = self.query(turn_ids=["t"], models=[LUNA])
        self.assertEqual(result["summary"]["usage_event_count"], 0)
        self.assertEqual(result["summary"]["running_seconds"], 600)
        self.assertEqual(result["turns"][0]["reasoning_efforts"], ["max"])
        self.assertEqual(result["diagnostics"]["turns_without_usage"], [{"session_id": "one", "turn_id": "t"}])

    def test_excluded_mixed_model_runtime_keeps_incomplete_and_aborted_status(self):
        self.write("unfinished", [started("unfinished-turn"), context("unfinished-turn"),
                                  token("2026-10-26T00:01:00Z"),
                                  context("unfinished-turn", SOL, "2026-10-26T00:02:00Z"),
                                  token("2026-10-26T00:03:00Z")])
        self.write("aborted", [started("aborted-turn"), context("aborted-turn"),
                               token("2026-10-26T00:01:00Z"),
                               context("aborted-turn", SOL, "2026-10-26T00:02:00Z"),
                               token("2026-10-26T00:03:00Z"),
                               item("event_msg", "2026-10-26T00:04:00Z", type="turn_aborted", turn_id="aborted-turn")])
        result = self.query(models=[LUNA])
        self.assertEqual(result["summary"]["usage_event_count"], 2)
        self.assertEqual(result["summary"]["summed_running_seconds"], 0)
        self.assertEqual(result["running_intervals"], [])
        self.assertEqual(result["diagnostics"]["incomplete_turns"], [{
            "session_id": "unfinished", "turn_id": "unfinished-turn", "runtime_included": False,
        }])
        self.assertEqual(result["diagnostics"]["aborted_turns"], [{
            "session_id": "aborted", "turn_id": "aborted-turn", "runtime_included": False,
        }])
        turns = {row["session_id"]: row for row in result["turns"]}
        self.assertEqual(turns["unfinished"]["statuses"], ["incomplete"])
        self.assertEqual(turns["aborted"]["statuses"], ["aborted"])
        excluded = {row["session_id"]: row for row in result["diagnostics"]["excluded_runtime"]}
        self.assertEqual(excluded["unfinished"]["status"], "incomplete")
        self.assertEqual(excluded["unfinished"]["timing_source"], "last_observed_event")
        self.assertEqual(excluded["unfinished"]["observed_end"], "2026-10-26T00:03:00+00:00")
        self.assertEqual(excluded["aborted"]["status"], "aborted")
        self.assertEqual(excluded["aborted"]["observed_end"], "2026-10-26T00:04:00+00:00")

    def test_later_known_model_does_not_make_earlier_unknown_usage_runtime_known(self):
        self.write("one", [started("t"), token("2026-10-26T00:01:00Z"),
                           context("t", LUNA, "2026-10-26T00:02:00Z"),
                           token("2026-10-26T00:03:00Z"), completed("t")])
        result = self.query(models=[LUNA])
        self.assertEqual(result["summary"]["usage_event_count"], 1)
        self.assertEqual(result["summary"]["running_seconds"], 0)
        self.assertEqual(result["summary"]["summed_running_seconds"], 0)
        self.assertEqual(result["diagnostics"]["excluded_runtime"][0]["reason"], "unknown_turn_model")
        self.assertEqual(result["turns"][0]["statuses"], ["completed"])
        # Without a model filter the entire observed turn remains measurable.
        self.assertEqual(self.query()["summary"]["running_seconds"], 600)

    def test_missing_session_id_is_explicit_without_inventing_an_identity(self):
        path = self.home / "sessions" / "missing-header.jsonl"
        records = [context("known-turn"), token("2026-10-26T00:01:00Z")]
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        result = self.query()
        self.assertEqual(result["diagnostics"]["events_without_session_id"], 1)
        self.assertEqual(result["summary"]["total_tokens"], 120)
        self.assertEqual(result["events"][0]["session_id"], "")
        self.assertEqual(result["matches"]["session_ids"], [])

    def test_cost_is_optional_and_unknown_or_total_only_usage_stays_unpriced(self):
        self.write("known", [context("known"), token("2026-10-26T00:01:00Z", input_tokens=1000, cached_input_tokens=200, output_tokens=100)])
        self.write("unknown", [context("unknown", "unpriced-model"), token("2026-10-26T00:02:00Z", input_tokens=50, output_tokens=10)])
        self.write("total-only", [context("total-only"), token("2026-10-26T00:03:00Z", input_tokens=0, output_tokens=0, total_tokens=77)])
        self.assertNotIn("cost", self.query())
        cost = self.query(include_cost=True)["cost"]
        self.assertAlmostEqual(cost["estimated_cost_usd"], 0.000132)
        self.assertEqual(cost["unpriced_tokens"], 137)
        self.assertEqual(cost["unpriced_usage_event_count"], 2)
        unknown = next(row for row in cost["models"] if row["model"] == "unpriced-model")
        self.assertIsNone(unknown["estimated_cost_usd"])
        self.assertIsNone(unknown["recorded_price"])
        self.assertIn("source", next(row for row in cost["models"] if row["model"] == LUNA)["recorded_price"])
        self.assertEqual(unknown["official_price_history"], [])

    def test_cost_uses_event_time_and_reports_the_price_history(self):
        from codex_usage.cost import ModelPrice, PricePeriod

        old = ModelPrice(1.0, 0.1, 2.0, source="old")
        new = ModelPrice(3.0, 0.3, 4.0, source="new")
        history = (PricePeriod(None, old), PricePeriod("2026-10-26T00:00:00Z", new))
        self.write("timed", [
            context("timed", stamp="2026-10-25T23:00:00Z"),
            token("2026-10-25T23:59:59Z", input_tokens=100, output_tokens=10),
            token("2026-10-26T08:00:00+08:00", input_tokens=100, output_tokens=10),
        ])
        with patch.dict("codex_usage.cost.OFFICIAL_PRICE_HISTORY", {LUNA: history}), \
             patch.dict("codex_usage.cost.OFFICIAL_PRICES", {LUNA: new}):
            row = self.query(include_cost=True)["cost"]["models"][0]
        self.assertAlmostEqual(row["estimated_cost_usd"], (120 + 340) / 1_000_000)
        self.assertEqual(row["priced_usage_event_count"], 2)
        self.assertEqual(row["unpriced_usage_event_count"], 0)
        self.assertEqual(row["recorded_price"]["source"], "new")
        self.assertEqual([period["price"]["source"] for period in row["official_price_history"]], ["old", "new"])
        self.assertIsNone(row["official_price_history"][0]["effective_from"])
        self.assertEqual(row["official_price_history"][1]["effective_from"], "2026-10-26T00:00:00Z")

    def test_zero_token_events_are_priced_only_for_known_models(self):
        import pandas as pd
        from codex_usage.query import _cost

        zero = {"timestamp": "2026-10-26T00:00:00Z", "input_tokens": 0,
                "output_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "total_tokens": 0}
        events = pd.DataFrame([
            {**zero, "model": LUNA},
            {**zero, "model": LUNA, "total_tokens": 10},
            {**zero, "model": "unknown"},
        ])
        result = _cost(events)
        known = next(row for row in result["models"] if row["model"] == LUNA)
        unknown = next(row for row in result["models"] if row["model"] == "unknown")
        self.assertEqual(known["estimated_cost_usd"], 0.0)
        self.assertEqual(known["priced_usage_event_count"], 1)
        self.assertEqual(known["unpriced_usage_event_count"], 1)
        self.assertEqual(known["unpriced_tokens"], 10)
        self.assertIsNone(unknown["estimated_cost_usd"])
        self.assertEqual(unknown["unpriced_usage_event_count"], 1)
        self.assertEqual(result["estimated_cost_usd"], 0.0)

    def test_filtering_does_not_trim_cache_and_incremental_append_matches_full_scan(self):
        self.tree()
        cache = self.root / "query.sqlite3"
        main = self.query(session_ids=["main"], cache_path=cache, incremental=True)
        leaf = self.query(session_ids=["leaf"], cache_path=cache, incremental=True)
        self.assertEqual(main["summary"]["total_tokens"], 120)
        self.assertEqual(leaf["summary"]["total_tokens"], 220)
        self.assertEqual(leaf["diagnostics"]["parse_stats"]["scan"]["bytes_read"], 0)
        with (self.home / "sessions" / "leaf.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(token("2026-10-26T00:09:00Z", input_tokens=400)) + "\n")
        incremental = self.query(thread_ids=["main"], cache_path=cache, incremental=True)
        fresh = self.query(thread_ids=["main"])
        for key in ("summary", "events", "sessions", "turns", "matches", "running_intervals"):
            self.assertEqual(incremental[key], fresh[key], key)
        self.assertEqual(incremental["summary"]["total_tokens"], 760)

    def test_archive_switch_and_model_metadata_are_preserved(self):
        self.write("old", [context("t", effort="high"), token("2026-10-26T00:01:00Z")], archived=True)
        included = self.query()
        self.assertEqual(included["events"][0]["reasoning_effort"], "high")
        self.assertEqual(included["summary"]["total_tokens"], 120)
        self.assertEqual(self.query(include_archived=False)["summary"]["total_tokens"], 0)

    def test_malformed_log_marks_partial_result_and_missing_runtime_is_explicit(self):
        path = self.write("one", [context("t"), token("2026-10-26T00:01:00Z")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{broken json}\n")
        result = self.query()
        self.assertFalse(result["diagnostics"]["complete"])
        self.assertEqual(result["summary"]["total_tokens"], 120)
        self.assertEqual(result["diagnostics"]["missing_runtime"], [{"session_id": "one", "turn_id": "t"}])

    def test_query_does_not_render_or_write_outputs_or_cache_by_default(self):
        self.write("one", [context("t"), token("2026-10-26T00:01:00Z")])
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        with patch("codex_usage.charts.export_report", side_effect=AssertionError("rendered report")), \
             patch("codex_usage.charts.save_figure", side_effect=AssertionError("rendered picture")):
            result = self.query()
        after = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.assertEqual(after, before)
        self.assertEqual(result["schema_version"], 1)
        self.assertIn("version", result["tool"])
        self.assertIsNone(result["filters"]["cache_path"])

    def test_dst_gap_and_fold_require_offsets(self):
        for value in ("2026-03-08T02:30:00", "2026-11-01T01:30:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.query(start=value, timezone="America/New_York")
        # An explicit offset makes the repeated clock time unambiguous.
        explicit = self.query(start="2026-11-01T01:30:00-04:00", end="2026-11-01T01:30:00-05:00", timezone="America/New_York")
        self.assertEqual(explicit["filters"]["start"], "2026-11-01T01:30:00-04:00")
        self.assertEqual(explicit["filters"]["end"], "2026-11-01T01:30:00-05:00")

    def test_invalid_ranges_and_bare_id_strings_are_rejected(self):
        for options in ({"session_ids": "one"}, {"models": [""]}, {"start": "bad"},
                        {"start": "2026-10-26T00:00:00Z", "end": "2026-10-26T00:00:00Z"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.query(**options)


if __name__ == "__main__":
    unittest.main()
