import contextlib
import datetime as dt
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import monitor
import network_monitor
import stats
import storage


BASE = 20000 * storage.DAY


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'samples %?.db'
        self.writer = storage.Writer(self.path)
        self.addCleanup(self.writer.close)

    def summary(self, interface, start=BASE, end=BASE + storage.DAY):
        with storage.reader(self.path) as connection:
            tables = storage.tables_for(connection, interface, start, end)
            return storage.summarize(connection, tables, start, end)

    def test_partition_rollover_and_interface_isolation(self):
        for stamp in (BASE - 1, BASE, BASE + 1, BASE + storage.DAY):
            self.writer.write(stamp, 1, {'eth0': (100, 200), 'wlan0': (300, 400)})
        with storage.reader(self.path) as connection:
            partitions = connection.execute('SELECT count(*) FROM partitions').fetchone()[0]
            self.assertEqual(partitions, 6)
            tables = storage.tables_for(connection, 'eth0', BASE, BASE + storage.DAY)
            self.assertEqual(tables, [storage.table_name('eth0', BASE // storage.DAY)])
            summary = storage.summarize(connection, tables, BASE, BASE + storage.DAY)
            self.assertEqual(summary.count, 2)
            self.assertEqual(summary.totals, [200, 400, 600])
            query = 'EXPLAIN QUERY PLAN SELECT * FROM "{}" WHERE timestamp >= ? AND timestamp < ?'.format(tables[0])
            plan = connection.execute(query, (BASE, BASE + storage.DAY)).fetchall()
            self.assertTrue(any('USING INDEX' in row[-1] for row in plan), plan)

    def test_partition_names_cannot_collide_or_inject_sql(self):
        names = ['eth-0', 'eth_0', '网卡', '"; DROP TABLE partitions; --']
        self.writer.write(BASE, 1, {name: (1, 2) for name in names})
        self.assertEqual(len({storage.table_name(name, 20000) for name in names}), len(names))
        for name in names:
            self.assertEqual(self.summary(name).count, 1)

    def test_half_open_range_and_weighted_sql_aggregates(self):
        self.writer.write(BASE + 1, 1, {'eth0': (1000000, 2000000)})
        self.writer.write(BASE + 4, 3, {'eth0': (6000000, 3000000)})
        summary = self.summary('eth0')
        self.assertEqual(summary.duration, 4)
        self.assertEqual(summary.minima, [8, 8, 24])
        self.assertEqual(summary.maxima, [16, 16, 24])
        self.assertEqual(summary.totals[0] * 8 / summary.duration / 1e6, 14)
        self.assertEqual(self.summary('eth0', BASE + 1, BASE + 4).count, 1)
        with storage.reader(self.path) as connection:
            tables = storage.tables_for(connection, 'eth0', BASE, BASE + storage.DAY)
            _, _, _, series = storage.chart_series(connection, tables, BASE, BASE + storage.DAY, summary, 1)
            self.assertEqual(series, [[14], [10], [24]])

    def test_chart_combines_partitions_without_loading_individual_rows(self):
        self.writer.write(BASE - 1, 1, {'eth0': (1000000, 2000000)})
        self.writer.write(BASE + 1, 3, {'eth0': (6000000, 3000000)})
        with storage.reader(self.path) as connection:
            tables = storage.tables_for(connection, 'eth0', BASE - 2, BASE + 2)
            summary = storage.summarize(connection, tables, BASE - 2, BASE + 2)
            queries = []
            connection.set_trace_callback(queries.append)
            chart = storage.chart_series(connection, tables, BASE - 2, BASE + 2, summary, 10)
            self.assertEqual([len(values) for values in chart[-1]], [10, 10, 10])
            self.assertTrue(all('GROUP BY' in query for query in queries))

    def test_round_is_atomic_when_a_second_interface_fails_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.writer.write(BASE, 1, {'eth0': (10, 20), 'wlan0': (-1, 20)})
        with storage.reader(self.path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM partitions').fetchone()[0], 0)
        self.writer.write(BASE, 1, {'eth0': (10, 20), 'wlan0': (30, 40)})
        self.assertEqual(self.summary('eth0').count, 1)
        self.assertEqual(self.summary('wlan0').count, 1)

    def test_wal_reader_has_consistent_snapshot_while_collector_writes(self):
        self.writer.write(BASE, 1, {'eth0': (10, 20)})
        with storage.reader(self.path) as connection:
            tables = storage.tables_for(connection, 'eth0', BASE, BASE + 10)
            self.assertEqual(storage.summarize(connection, tables, BASE, BASE + 10).count, 1)
            self.writer.write(BASE + 1, 1, {'eth0': (30, 40)})
            self.assertEqual(storage.summarize(connection, tables, BASE, BASE + 10).count, 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM partitions')
        self.assertEqual(self.summary('eth0').count, 2)

    @unittest.skipIf(os.geteuid() == 0, 'root 不受目录写权限限制')
    def test_unprivileged_reader_can_query_stopped_service_database(self):
        import fcntl
        self.writer.write(BASE, 1, {'eth0': (10, 20)})
        self.writer.close()
        lock = Path(str(self.path) + '.lock')
        lock.touch()
        self.path.chmod(0o444)
        self.path.parent.chmod(0o555)
        try:
            with storage.reader(self.path) as connection:
                tables = storage.tables_for(connection, 'eth0', BASE, BASE + 10)
                self.assertEqual(storage.summarize(connection, tables, BASE, BASE + 10).totals, [10, 20, 30])
                with lock.open('rb') as competitor, self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute('DELETE FROM partitions')
            self.assertFalse(Path(str(self.path) + '-wal').exists())
            with lock.open('rb') as competitor:
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(sqlite3.OperationalError):
                    with storage.reader(self.path):
                        self.fail('采集锁被占用时不得使用不可变数据库模式')
        finally:
            self.path.parent.chmod(0o755)
            self.path.chmod(0o644)

    def test_existing_unrelated_database_is_not_modified(self):
        path = self.path.parent / 'other.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE existing (value INTEGER)')
        with self.assertRaisesRegex(RuntimeError, '不是 network-monitor'):
            storage.Writer(path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(), [('existing',)])

    def test_default_interface_is_used_by_monitor_and_stats(self):
        config = self.path.parent / 'empty.ini'
        config.write_text('', encoding='utf-8')
        with patch('monitor.default_interface', return_value='eth0') as default, \
                patch('monitor.counters', side_effect=[(100, 200), (110, 220)]), \
                patch('monitor.time.sleep'), patch('monitor.time.monotonic', side_effect=[0, 0, 1]), \
                patch('monitor.time.time', return_value=BASE + 1), contextlib.redirect_stdout(io.StringIO()):
            result = network_monitor.main(['monitor', '-c', str(config), '-d', str(self.path), '--count', '1'])
            self.assertEqual(result, 0)
            default.assert_called_once_with()
        output = io.StringIO()
        with patch('monitor.default_interface', return_value='eth0') as default, contextlib.redirect_stdout(output):
            result = network_monitor.main(['stats', '-c', str(config), '-d', str(self.path),
                                           '--start', str(BASE), '--end', str(BASE + 10), '--no-chart'])
            self.assertEqual(result, 0)
            default.assert_called_once_with()
        self.assertIn('采样：1 次', output.getvalue())

    def test_counter_reset_on_one_interface_does_not_drop_the_other(self):
        parser, _ = network_monitor.build_parser()
        args = parser.parse_args(['monitor', '-i', 'eth0', 'wlan0', '-d', str(self.path), '--count', '2'])
        counters = {'eth0': iter([(100, 200), (90, 250), (100, 260)]),
                    'wlan0': iter([(500, 1000), (520, 1040), (540, 1080)])}
        with patch('monitor.counters', side_effect=lambda interface: next(counters[interface])), \
                patch('monitor.time.sleep'), patch('monitor.time.monotonic', side_effect=[0, 0, 1, 1, 2]), \
                patch('monitor.time.time', side_effect=[BASE - 1, BASE]), contextlib.redirect_stdout(io.StringIO()):
            monitor.run(args, args.command_parser)
        self.assertEqual(self.summary('eth0', BASE - 10, BASE + 10).totals, [10, 10, 20])
        summary = self.summary('wlan0', BASE - 10, BASE + 10)
        self.assertEqual(summary.count, 2)
        self.assertEqual(summary.totals, [80, 40, 120])

    def test_interface_flag_replaces_config_and_deduplicates(self):
        parser, commands = network_monitor.build_parser()
        commands['monitor'].set_defaults(interface='configured0, configured1')
        args = parser.parse_args(['monitor', '-i', 'eth0,eth1', '-i', 'eth0', 'eth2'])
        monitor.validate(args, args.command_parser)
        self.assertEqual(args.interface, ['eth0', 'eth1', 'eth2'])

    def test_narrow_chart_axis_can_render_multiple_days(self):
        axis, labels = stats.time_axis(BASE, BASE + storage.DAY, 10, dt.timezone.utc)
        self.assertEqual(len(axis), 20)
        self.assertEqual(len(labels), 20)


if __name__ == '__main__':
    unittest.main()
