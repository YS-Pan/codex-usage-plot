"""Legacy upgrades preserve checkpoints and fail without rescanning history."""

import hashlib
import io
import json
import sqlite3
import threading
import unittest
from contextlib import closing, redirect_stderr
from unittest.mock import patch

from codex_usage.cache import (
    CACHE_VERSION, CacheMigrationError, UsageCache, json_text, migration_guard,
)
from codex_usage.charts import main as report_main, parse_rollout
from tests import test_codex_usage_cache as fixtures
from tests.test_codex_usage_cache import rollout, tokens


class CacheMigrationTests(unittest.TestCase):
    setUp = fixtures.IncrementalUsageTests.setUp
    write = fixtures.IncrementalUsageTests.write
    read = fixtures.IncrementalUsageTests.read
    assert_fresh = fixtures.IncrementalUsageTests.assert_fresh
    cache_snapshot = fixtures.IncrementalUsageTests.cache_snapshot

    def legacy(self):
        self.write('a.jsonl', rollout())
        self.write('b.jsonl', [tokens('2026-08-30T01:02:00Z', total=80)])
        self.read()
        with UsageCache(self.cache, self.home) as cache:
            entries = cache.entries()
            active = set(entries)
            events = list(cache.load_events(active))
        with closing(sqlite3.connect(self.cache)) as connection:
            connection.execute('ALTER TABLE events RENAME TO compact_events')
            connection.execute('CREATE TABLE events (sequence INTEGER PRIMARY KEY, log_file TEXT NOT NULL, record TEXT NOT NULL, fingerprint TEXT NOT NULL, FOREIGN KEY(log_file) REFERENCES logs(log_file) ON DELETE CASCADE ON UPDATE CASCADE)')
            compact = connection.execute('SELECT sequence, log_file FROM compact_events ORDER BY log_file COLLATE NOCASE, sequence').fetchall()
            for (sequence, log), (record, fingerprint) in zip(compact, events):
                stored = {**record, 'timestamp': record['timestamp'].isoformat()}
                connection.execute('INSERT INTO events VALUES (?, ?, ?, ?)', (sequence, log, json_text(stored), json_text(fingerprint)))
            connection.execute('DROP TABLE compact_events')
            connection.execute('CREATE INDEX events_log ON events(log_file)')
            connection.execute('CREATE INDEX events_read_order ON events(log_file COLLATE NOCASE, sequence)')
            for sequence, log, text in connection.execute('SELECT * FROM intervals').fetchall():
                connection.execute('UPDATE intervals SET record=? WHERE sequence=?', (json_text({**json.loads(text), 'log_file': log}), sequence))
            connection.execute('PRAGMA user_version = 3')
            connection.commit()
        return entries, events

    def digest(self):
        return hashlib.sha256(self.cache.read_bytes()).hexdigest()

    def test_legacy_upgrade_preserves_every_record_and_avoids_log_parse(self):
        entries, events = self.legacy()
        before = self.cache_snapshot()
        with patch('codex_usage.charts.parse_rollout', wraps=parse_rollout) as parser:
            result = self.read()
            parser.assert_not_called()
        self.assert_fresh(result)
        self.assertEqual(result[2].scan.bytes_read, 0)
        with UsageCache(self.cache, self.home) as cache:
            self.assertEqual(entries, cache.entries())
            self.assertEqual(events, list(cache.load_events(set(entries))))
        backups = list(self.root.glob('usage.sqlite3.v3-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)
            for table in ('logs', 'events', 'intervals'):
                self.assertEqual(before[table], connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall())
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], CACHE_VERSION)
            self.assertEqual([row[1] for row in connection.execute('PRAGMA table_info(events)')], ['sequence', 'log_file', 'record'])
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue({'events_log', 'events_read_order', 'intervals_log', 'intervals_read_order'} <= indexes)
            stored = json.loads(connection.execute('SELECT record FROM events').fetchone()[0])
            self.assertNotIn('log_file', stored)
            self.assertNotIn('uncached_input_tokens', stored)
        self.assertFalse(list(self.root.glob('usage.sqlite3.invalid-*')))

    def test_first_migrated_connection_keeps_the_new_cache_page_policy(self):
        self.legacy()
        with UsageCache(self.root / 'fresh.sqlite3', self.home) as fresh:
            expected = fresh.connection.execute('PRAGMA cache_size').fetchone()[0]
        # Inspect the first context immediately after the automatic migration;
        # a later ordinary open would hide a missing reconnect configuration.
        with UsageCache(self.cache, self.home) as upgraded:
            actual = upgraded.connection.execute('PRAGMA cache_size').fetchone()[0]
            self.assertEqual(actual, expected)

    def test_upgrade_io_failure_and_interruption_leave_original_and_can_retry(self):
        for failure in (OSError('simulated storage failure'), KeyboardInterrupt('cancelled')):
            with self.subTest(failure=type(failure).__name__):
                self.cache.unlink(missing_ok=True)
                self.legacy()
                before = self.digest()
                with patch.object(UsageCache, '_verify_migration', side_effect=failure):
                    with patch('codex_usage.charts.parse_rollout') as parser:
                        with self.assertRaises(type(failure)):
                            self.read()
                        parser.assert_not_called()
                self.assertEqual(self.digest(), before)
                self.assertFalse(list(self.root.glob('usage.sqlite3.invalid-*')))
                self.assertFalse(list(self.root.glob('usage.sqlite3.migrating-*')))
                with patch('codex_usage.charts.parse_rollout', wraps=parse_rollout) as parser:
                    self.assert_fresh(self.read())
                    # assert_fresh reads synthetic source once, the incremental retry does not.
                    self.assertEqual(parser.call_count, 2)

    def test_restored_fingerprint_path_fallback_tracks_archive_move(self):
        self.legacy()
        self.read()
        path = self.sessions / 'b.jsonl'
        path.rename(self.archived / path.name)
        result = self.read()
        self.assert_fresh(result)
        with UsageCache(self.cache, self.home) as cache:
            records = list(cache.load_events(set(cache.entries())))
        anonymous = [item for item in records if not item[0]['session_id']][0]
        self.assertEqual(anonymous[1][0], str((self.archived / 'b.jsonl').resolve()))
        self.assertEqual(result[2].scan.files_moved, 1)
        self.assertEqual(result[2].scan.bytes_read, 0)

    def test_missing_original_counter_rebuilds_instead_of_inventing_zero(self):
        self.write()
        self.read()
        with closing(sqlite3.connect(self.cache)) as connection:
            record = json.loads(connection.execute('SELECT record FROM events').fetchone()[0])
            del record['cached_input_tokens']
            connection.execute('UPDATE events SET record=?', (json_text(record),))
            connection.commit()
        self.assert_fresh(self.read())
        self.assertEqual(len(list(self.root.glob('usage.sqlite3.invalid-*'))), 1)

    def test_malformed_legacy_record_follows_invalid_backup_contract(self):
        self.legacy()
        with closing(sqlite3.connect(self.cache)) as connection:
            connection.execute("UPDATE events SET record='[]'")
            connection.commit()
        self.assert_fresh(self.read())
        self.assertEqual(len(list(self.root.glob('usage.sqlite3.invalid-*'))), 1)

    def test_incomplete_legacy_schema_is_invalid_even_with_no_event_rows(self):
        for keep_events in (False, True):
            with self.subTest(keep_events=keep_events):
                self.cache.unlink(missing_ok=True)
                self.legacy()
                with closing(sqlite3.connect(self.cache)) as connection:
                    if not keep_events:
                        connection.execute('DELETE FROM events')
                    connection.execute('ALTER TABLE events DROP COLUMN fingerprint')
                    connection.commit()
                backups_before = len(list(self.root.glob('usage.sqlite3.invalid-*')))
                self.assert_fresh(self.read())
                self.assertEqual(len(list(self.root.glob('usage.sqlite3.invalid-*'))), backups_before + 1)

    def test_legacy_orphan_events_follow_the_invalid_cache_recovery_contract(self):
        self.legacy()
        with closing(sqlite3.connect(self.cache)) as connection:
            connection.execute("UPDATE events SET log_file='sessions/missing.jsonl'")
            connection.commit()
        self.assert_fresh(self.read())
        self.assertEqual(len(list(self.root.glob('usage.sqlite3.invalid-*'))), 1)

    def test_os_migration_lock_releases_and_existing_marker_is_harmless(self):
        self.legacy()
        lock = self.cache.with_name(self.cache.name + '.migrate.lock')
        with migration_guard(lock):
            with self.assertRaises(CacheMigrationError):
                with UsageCache(self.cache, self.home):
                    pass
        self.assertTrue(lock.exists())
        self.assert_fresh(self.read())

    def test_switch_interruption_rolls_back_schema_before_commit(self):
        self.legacy()
        before = self.digest()

        def interrupted(cache, source):
            source.execute('DROP TABLE events')
            raise KeyboardInterrupt('cancelled before format commit')

        with patch.object(UsageCache, '_copy_migration_tables', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.read()
        self.assertEqual(self.digest(), before)
        self.assertFalse(list(self.root.glob('usage.sqlite3.migrating-*')))
        self.assert_fresh(self.read())

    def test_compaction_failure_keeps_upgraded_data_and_legacy_backup(self):
        self.legacy()
        with patch.object(UsageCache, '_compact_after_migration', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(CacheMigrationError, '格式已升级.*空间整理未完成'):
                self.read()
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], CACHE_VERSION)
        backups = list(self.root.glob('usage.sqlite3.v3-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        with patch('codex_usage.charts.parse_rollout', wraps=parse_rollout) as parser:
            result = self.read()
            parser.assert_not_called()
        self.assert_fresh(result)
        self.assertFalse(list(self.root.glob('usage.sqlite3.invalid-*')))

    def test_legacy_writer_waits_through_switch_and_successful_commit_survives(self):
        self.legacy()
        entered, finished = threading.Event(), threading.Event()
        errors = []

        def writer():
            try:
                with closing(sqlite3.connect(self.cache, timeout=5)) as connection:
                    entered.set()
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute("INSERT INTO metadata VALUES ('concurrent-writer', 'committed')")
                    connection.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        original = UsageCache._copy_migration_tables
        threads = []

        def switching(cache, source):
            self.assertTrue(source.in_transaction)
            thread = threading.Thread(target=writer)
            threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(0.05), 'Legacy writer crossed the format-switch lock')
            original(cache, source)
            self.assertTrue(source.in_transaction)

        with patch.object(UsageCache, '_copy_migration_tables', switching):
            self.read()
        for thread in threads:
            thread.join(6)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='concurrent-writer'").fetchone(), ('committed',))
        self.assert_fresh(self.read())

    def test_report_cache_migration_failure_is_a_readable_cli_error(self):
        self.write()
        error = io.StringIO()
        with patch('codex_usage.charts.collect_usage_data', side_effect=CacheMigrationError('升级未完成，原数据保留')):
            with redirect_stderr(error):
                result = report_main(['--codex-home', str(self.home), '--no-animations', '--quiet'])
        self.assertEqual(result, 1)
        self.assertIn('读取日志或缓存失败', error.getvalue())
        self.assertIn('原数据保留', error.getvalue())
