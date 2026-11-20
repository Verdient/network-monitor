import datetime as dt
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

import storage

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ([str(Path(os.environ['NETWORK_MONITOR_BINARY']).resolve())]
           if 'NETWORK_MONITOR_BINARY' in os.environ
           else [sys.executable, str(ROOT / 'network_monitor.py')])
STAMP = int(dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc).timestamp())
QUERY = ('--start', '2026-11-19', '--end', '2026-11-20', '--timezone', 'UTC', '-i', 'eth0')


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = Path(self.directory.name)
        self.env = dict(os.environ, HOME=str(self.cwd / 'home'),
                        XDG_CONFIG_HOME=str(self.cwd / 'config'),
                        XDG_STATE_HOME=str(self.cwd / 'state'))

    def invoke(self, *arguments):
        return subprocess.run(COMMAND + list(arguments), cwd=self.cwd, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def samples(self, name='samples.db', interface='eth0'):
        path = self.cwd / name
        writer = storage.Writer(path)
        try:
            writer.write(STAMP + 1, 1, {interface: (1000000, 2000000)})
            writer.write(STAMP + 4, 3, {interface: (6000000, 3000000)})
        finally:
            writer.close()
        return path

    def config(self, content, name='custom.ini'):
        (self.cwd / name).write_text(content, encoding='utf-8')
        return name

    def stats_config(self, database='samples.db', name='custom.ini', extra=''):
        return self.config('[monitor]\ndatabase = {}\n[stats]\ntimezone=UTC\n{}'.format(database, extra), name)

    def test_help_includes_commands_and_config_priority(self):
        for command in ((), ('monitor',), ('stats',), ('install',)):
            with self.subTest(command=command):
                result = self.invoke(*command, '-h')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage: network-monitor', result.stdout)
                self.assertIn('命令行 > 配置文件 > 内置默认值', result.stdout)
                self.assertIn('示例' if command else '快速开始', result.stdout)

    def test_help_survives_bad_configuration(self):
        self.config('invalid', 'network-monitor.ini')
        self.assertEqual(self.invoke('stats', '-c', 'network-monitor.ini', '-h').returncode, 0)

    def test_command_required_and_unknown_command_rejected(self):
        for arguments in ((), ('unknown',)):
            result = self.invoke(*arguments)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)

    def test_config_path_before_and_after_command(self):
        self.samples()
        self.stats_config()
        for arguments in (('-c', 'custom.ini', 'stats'), ('stats', '-c', 'custom.ini')):
            result = self.invoke(*arguments, *QUERY, '--no-chart')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('采样：2 次', result.stdout)
            self.assertNotIn('网速趋势', result.stdout)
        result = self.invoke('stats', '-c', 'custom.ini', *QUERY, '--chart', '--width', '20', '--height', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('网速趋势', result.stdout)

    def test_auto_config_uses_platform_location_and_ignores_working_directory(self):
        self.samples()
        relative = Path('home/Library/Application Support/network-monitor' if platform.system() == 'Darwin'
                        else 'config/network-monitor')
        directory = self.cwd / relative
        directory.mkdir(parents=True)
        self.stats_config(name=str(relative / 'config.ini'))
        result = self.invoke('stats', *QUERY, '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stats_config(database='local.db', name='network-monitor.ini')
        result = self.invoke('stats', *QUERY, '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke('stats', '-c', 'network-monitor.ini', *QUERY)
        self.assertEqual(result.returncode, 1)
        self.assertIn('local.db', result.stderr)

    def test_explicit_database_overrides_service_database(self):
        self.samples()
        self.stats_config(database='missing-monitor.db')
        missing = self.invoke('stats', '-c', 'custom.ini', *QUERY)
        self.assertEqual(missing.returncode, 1)
        self.assertIn('missing-monitor.db', missing.stderr)
        result = self.invoke('stats', '-c', 'custom.ini', '-d', 'samples.db', *QUERY, '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_configuration_and_missing_explicit_path(self):
        self.assertEqual(self.invoke('stats', '-c', 'missing.ini').returncode, 2)
        for content in ('invalid', '[typo]\ncount=1', '[monitor]\ncounnt=1',
                        '[stats]\nheight=many', '[stats]\nno_chart=maybe',
                        '[monitor]\ndatabase=', '[DEFAULT]\ncount=1'):
            with self.subTest(content=content):
                self.config(content)
                result = self.invoke('stats', '-c', 'custom.ini')
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('Traceback', result.stderr)

    def test_monitor_minimum_interval_is_one_second(self):
        for arguments in (('--interval', '0'), ('--interval', '0.99'), ('--interval', '-1'),
                          ('--interval', 'nan'), ('--interval', 'inf'), ('--count', '-1')):
            result = self.invoke('monitor', *arguments)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_bad_stats_options_fail_before_querying(self):
        for arguments in (('--width', '9'), ('--height', '2'), ('--height', '31'),
                          ('--timezone', 'Mars/Olympus'), ('--start', 'nan'),
                          ('--start', 'nonsense'),
                          ('--start', '2026-11-20', '--end', '2026-11-19')):
            result = self.invoke('stats', *arguments)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_missing_database_does_not_create_it(self):
        result = self.invoke('stats', '-d', 'missing.db', '-i', 'eth0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing.db', result.stderr)
        self.assertFalse((self.cwd / 'missing.db').exists())

    def test_stats_weighted_averages_and_chart(self):
        self.samples()
        result = self.invoke('stats', '-d', 'samples.db', *QUERY, '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('14.00', result.stdout)
        self.assertIn('10.00', result.stdout)
        self.assertNotIn('网速趋势', result.stdout)
        result = self.invoke('stats', '-d', 'samples.db', *QUERY, '--width', '20', '--height', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('网速趋势', result.stdout)

    def test_stats_defaults_to_system_timezone_and_today(self):
        self.env['TZ'] = 'Asia/Tokyo'
        zone = ZoneInfo('Asia/Tokyo')
        today = dt.datetime.fromtimestamp(time.time(), zone).replace(hour=0, minute=0, second=0, microsecond=0)
        writer = storage.Writer(self.cwd / 'network-monitor.db')
        try:
            for stamp in (int(today.timestamp()) - 1, int(today.timestamp()) + 1,
                          int((today + dt.timedelta(days=1)).timestamp())):
                writer.write(stamp, 1, {'eth0': (100, 200)})
        finally:
            writer.close()
        config = self.config('', 'empty.ini')
        result = self.invoke('stats', '-c', config, '-i', 'eth0', '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('采样：1 次', result.stdout)
        self.assertIn('+0900', result.stdout)
        self.assertIn('系统时区', result.stdout)

    def test_local_day_spans_two_utc_partitions_and_interface_is_filtered(self):
        zone = ZoneInfo('Asia/Shanghai')
        start = int(dt.datetime(2026, 11, 19, tzinfo=zone).timestamp())
        end = start + 86400
        writer = storage.Writer(self.cwd / 'samples.db')
        try:
            for stamp in (start - 1, start, start + 12 * 3600, end):
                writer.write(stamp, 1, {'eth0': (100, 200), 'wlan0': (500, 600)})
        finally:
            writer.close()
        result = self.invoke('stats', '-d', 'samples.db', '-i', 'wlan0', '--timezone',
                             'Asia/Shanghai', '--start', '2026-11-19', '--end', '2026-11-20', '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('采样：2 次', result.stdout)
        self.assertIn('1.00 KB', result.stdout)
        self.assertIn('1.20 KB', result.stdout)
        result = self.invoke('stats', '-d', 'samples.db', '-i', 'absent', *QUERY[:-2], '--no-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('暂无采样数据', result.stdout)

    def test_unix_and_offset_aware_times_select_the_same_records(self):
        self.samples()
        for start, end in ((str(STAMP + 1), str(STAMP + 4)),
                           ('2026-11-19T20:00:01+08:00', '2026-11-19T20:00:04+08:00')):
            result = self.invoke('stats', '-d', 'samples.db', '-i', 'eth0', '--start', start,
                                 '--end', end, '--timezone', 'UTC', '--no-chart')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('采样：1 次', result.stdout)
            self.assertIn('8.00', result.stdout)

    @unittest.skipUnless(platform.system() == 'Linux', 'uses Linux counters')
    def test_real_multi_interface_collection_and_config_override(self):
        names = [name for _, name in socket.if_nameindex() if name != 'lo']
        if not names:
            self.skipTest('needs two network interfaces')
        names = ['lo', names[0]]
        self.config('[monitor]\ninterface=missing\ninterval=1\ndatabase=config.db\n', 'network-monitor.ini')
        result = self.invoke('monitor', '-c', 'network-monitor.ini', '-i', names[0], '-i', names[1], '-d', 'traffic.db', '--count', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.cwd / 'config.db').exists())
        with storage.reader(self.cwd / 'traffic.db') as connection:
            for name in names:
                tables = storage.tables_for(connection, name, 0, 1e12)
                summary = storage.summarize(connection, tables, 0, 1e12)
                self.assertEqual(summary.count, 2)
                self.assertGreaterEqual(summary.duration, 2)
                for table in tables:
                    row = connection.execute('SELECT typeof(timestamp), typeof(duration_s) FROM "{}"'.format(table)).fetchone()
                    self.assertEqual(row, ('integer', 'real'))
        stats = self.invoke('stats', '-d', 'traffic.db', '-i', 'lo', '--no-chart')
        self.assertEqual(stats.returncode, 0, stats.stderr)
        self.assertIn('采样：2 次', stats.stdout)

    @unittest.skipUnless(platform.system() == 'Linux', 'uses fcntl')
    def test_duplicate_collector_fails_before_sampling(self):
        import fcntl
        with (self.cwd / 'traffic.db.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke('monitor', '-d', 'traffic.db', '-i', 'lo', '--count', '1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('已有采集进程', result.stderr)
        self.assertFalse((self.cwd / 'traffic.db').exists())

    def test_install_preview_saves_database_and_multiple_interfaces(self):
        self.config('[monitor]\ninterface=eth0, wlan0\ninterval=2\ndatabase=traffic.db\n')
        result = self.invoke('install', '--user', '-c', 'custom.ini', '--interval', '3', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('interface = eth0, wlan0', result.stdout)
        self.assertIn('traffic.db', result.stdout)
        self.assertIn('interval = 3.0', result.stdout)
        self.assertEqual([path.name for path in self.cwd.iterdir()], ['custom.ini'])
        result = self.invoke('install', '-c', 'custom.ini', '--system', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('系统服务', result.stdout)

    def test_install_rejects_finite_sampling(self):
        result = self.invoke('install', '--count', '3', '--dry-run')
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments', result.stderr)

    def test_temporary_and_install_options_are_rejected_in_configuration(self):
        for section, key, value in (('monitor', 'count', '1'), ('stats', 'start', '2026-11-19'),
                                    ('stats', 'end', '2026-11-20'), ('stats', 'no_chart', 'true'),
                                    ('stats', 'width', '80'), ('stats', 'height', '8'),
                                    ('stats', 'interface', 'eth0'), ('stats', 'database', 'other.db'),
                                    ('install', 'user', 'true'), ('install', 'work_dir', '/tmp/data')):
            with self.subTest(section=section, key=key):
                self.config('[{}]\n{}={}\n'.format(section, key, value))
                result = self.invoke('stats', '-c', 'custom.ini')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('命令行', result.stderr)


if __name__ == '__main__':
    unittest.main()
