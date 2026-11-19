"""Observed repeated-state boundary, reproduced with metadata-only fake identities."""

import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_usage.charts import collect_usage_data


UTC = ZoneInfo('UTC')


def event(kind, second, **payload):
    return {'type': kind, 'timestamp': f'2026-11-17T10:54:{second:02d}Z', 'payload': payload}


def usage(total=153114):
    if total == 153114:
        return {'input_tokens': 150241, 'cached_input_tokens': 146944,
                'cache_write_input_tokens': 0, 'output_tokens': 2873,
                'reasoning_output_tokens': 2713, 'total_tokens': 153114}
    return {'input_tokens': total-10, 'cached_input_tokens': 0,
            'cache_write_input_tokens': 0, 'output_tokens': 10,
            'reasoning_output_tokens': 0, 'total_tokens': total}


def count(second, values=None, total=None):
    info = {}
    if values is not None:
        info['last_token_usage'] = values
    if total is not None:
        info['total_token_usage'] = total
    return event('event_msg', second, type='token_count', info=info)


def response(second, identity, values, total=None):
    return event('token_usage_record', second, turn_id='turn', response_id=identity,
                 usage=values, thread_token_usage=total)


class ResponseIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root/'codex'
        (self.home/'sessions').mkdir(parents=True)
        self.path = self.home/'sessions/main.jsonl'
        self.cache = self.root/'cache.sqlite3'
        self.rows = [event('session_meta', 0, id='synthetic', cwd='D:/synthetic'),
                     event('turn_context', 0, turn_id='turn', model='gpt-6-luna')]

    def write(self, records):
        self.rows.extend(records)
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in self.rows), encoding='utf-8')

    def read(self, incremental=False):
        return collect_usage_data(self.home, UTC, progress=False,
                                  cache_path=self.cache, incremental=incremental)

    def test_observed_repeated_last_and_unchanged_cumulative_counts_once(self):
        values = usage()
        total = {'input_tokens': 2648720, 'cached_input_tokens': 2514048,
                 'cache_write_input_tokens': 0, 'output_tokens': 40009,
                 'reasoning_output_tokens': 30302, 'total_tokens': 2688729}
        self.write([response(0, 'response-a', values, total), count(0, values, total),
                    count(27, values, total)])
        frame, _, stats = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [153114])
        self.assertEqual(stats.duplicate_events_removed, 1)

    def test_equal_sized_new_responses_remain_separate(self):
        values = usage(100)
        self.write([response(0, 'a', values, usage(100)), count(1, values, usage(100)),
                    response(2, 'b', values, usage(200)), count(3, values, usage(200))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100])

    def test_same_response_replayed_with_new_timestamp_is_deduplicated(self):
        values = usage(100)
        self.write([response(0, 'a', values, values), count(1, values, values),
                    response(2, 'a', values, values), count(3, values, values)])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100])

    def test_recent_unmatched_id_is_not_assigned_to_other_usage(self):
        self.write([response(0, 'a', usage(100), usage(100)),
                    count(1, usage(200), usage(200)), count(2, usage(200), usage(400))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [200, 200])

    def test_usage_match_without_cumulative_match_does_not_claim_identity(self):
        values = usage(100)
        self.write([response(0, 'a', values, usage(100)), count(1, values, usage(200)),
                    count(2, values, usage(300))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100])

    def test_new_response_id_wins_over_repeated_cumulative_state(self):
        values = usage(100)
        self.write([response(0, 'a', values, values), count(1, values, values),
                    response(2, 'b', values, values), count(3, values, values)])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100])

    def test_legacy_repeated_cumulative_state_is_removed(self):
        self.write([count(1, usage(100), usage(100)), count(2, usage(100), usage(100))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100])

    def test_without_identity_or_cumulative_equal_sizes_are_preserved(self):
        self.write([count(1, usage(100)), count(2, usage(100))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100])

    def test_without_cumulative_an_id_cannot_claim_equal_sized_counts(self):
        self.write([response(0, 'a', usage(100)), count(1, usage(100)),
                    response(2, 'a', usage(100)), count(3, usage(100))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100])

    def test_new_turn_and_counter_reset_are_not_duplicates(self):
        self.write([count(1, usage(100), usage(100)), count(2, usage(100), usage(200)),
                    count(3, usage(100), usage(100)),
                    event('turn_context', 4, turn_id='new-turn', model='gpt-6-luna'),
                    count(5, usage(100), usage(100))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100, 100, 100])

    def test_cumulative_only_retains_delta_and_reset_rules(self):
        self.write([count(1, total=usage(100)), count(2, total=usage(200)),
                    count(3, total=usage(200)), count(4, total=usage(50))])
        frame, _, _ = self.read()
        self.assertEqual(frame['total_tokens'].tolist(), [100, 100, 50])

    def test_incremental_resume_between_response_and_count_matches_full_read(self):
        values = usage(100)
        self.write([response(0, 'a', values, values)])
        self.assertTrue(self.read(incremental=True)[0].empty)
        self.write([count(1, values, values)])
        self.assertEqual(self.read(incremental=True)[0]['total_tokens'].tolist(), [100])
        self.write([count(2, values, values), response(3, 'b', values, usage(200)),
                    count(4, values, usage(200))])
        cached = self.read(incremental=True)
        fresh = self.read(incremental=False)
        self.assertEqual(cached[0]['total_tokens'].tolist(), [100, 100])
        self.assertEqual(cached[0].to_dict('records'), fresh[0].to_dict('records'))


if __name__ == '__main__':
    unittest.main()
