import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import network_monitor
from stats import LiveSampler, dashboard_text, live_update, read_report
from stats_display import ANSI, Dashboard, LiveSample, Report, area_cells, display_width
import storage
from tcp_connections import Connections


class HistoricalDashboardTests(unittest.TestCase):
    def test_metric_cards_and_chart_end_on_the_same_row(self):
        summary = storage.Summary()
        summary.add_sample(10, 1, 1000, 2000)
        chart = (0, 10, 1, [[0.1] * 10] * 3)
        report = Report('en0', None, 0, 10, summary, chart)
        for color in (True, False):
            with self.subTest(color=color):
                screen = Dashboard(120, color=color).render(report, None, rows=36, rail=36)
                lines = ANSI.sub('', screen).splitlines()
                self.assertIn('网速趋势', screen)
                self.assertRegex(lines[-1], r'^└.*┘ └.*┘$')
                self.assertRegex(lines[-2], r'\d{2}:\d{2}')


class LiveUpdateTests(unittest.TestCase):
    def test_full_and_partial_wave_cells_only_set_the_foreground(self):
        values = [0, 1, 1, 1, None, 1, 0.25, 0.75]
        chart = (0, 8, 1, [values] * 3)
        for mode in ({'truecolor': True}, {'indexed': True}, {}):
            display = Dashboard(80, color=True, **mode)
            for live in (False, True):
                graph = '\n'.join(display.graph(chart, 4, None, live))
                self.assertIn('⣿', graph)
                inks = re.findall(r'\x1b\[([0-9;]+)m[\u2801-\u28ff]+', graph)
                self.assertTrue(inks)
                self.assertLessEqual(set(inks), set(display.wave_inks['down'] + display.wave_inks['up']))

    def test_rail_positions_from_first_frame_through_socket_and_rate_updates(self):
        frames = [
            (Connections(status='读取中'), ()),
            (Connections(2, peers=(('::1', 2),)), (2,)),
            (Connections(12, peers=(('::1', 9), ('192.0.2.1', 3))), (2, 12)),
            (Connections(status='连接表不可用'), (2, 12, None)),
            (Connections(), (2, 12, None, 0)),
        ]
        markers = ('↓ 下载 · 当前', 'TCP 连接 · 本机', '已建立趋势', '已建立对端')
        with TemporaryDirectory() as directory, patch.dict(os.environ, COLORTERM='truecolor'):
            os.environ.pop('NO_COLOR', None)
            for has_history in (False, True):
                path = Path(directory) / ('history.db' if has_history else 'missing.db')
                if has_history:
                    writer = storage.Writer(path)
                    try:
                        writer.write(90, 1, {'en0': (1000, 2000)})
                    finally:
                        writer.close()
                options = SimpleNamespace(database=path, width=None, height=None, no_chart=False)
                for rows in (36, 47):
                    sampler = LiveSampler('en0')
                    sampler.started_at = 100
                    positions = []
                    for i, frame in enumerate(frames):
                        sampler.connections = frame
                        if i == 2:
                            sampler.samples.append((101, 1, 125000, 250000))
                            sampler.reading = LiveSample((1, 2, 3))
                        raw = dashboard_text(options, 'en0', None, (80, 102), True, sampler,
                                             os.terminal_size((122, rows)))
                        lines = ANSI.sub('', raw).splitlines()
                        positions.append(tuple(next(n for n, line in enumerate(lines) if marker in line)
                                               for marker in markers))
                        self.assertEqual(len(lines), rows)
                    self.assertEqual(len(set(positions)), 1, positions)

    def test_connection_pages_fit_and_reveal_the_remaining_ipv6_peers(self):
        peers = tuple(('2001:db8:ffff:ffff:ffff:ffff:ffff:{:x}'.format(i), 20 - i) for i in range(20))
        frame = Connections(210, 4, 2, 0, peers), (10, None, 210)
        display = Dashboard(84)
        first = display.connection_screen(frame, 17, 0)
        second = display.connection_screen(frame, 17, 1)
        self.assertIn(peers[0][0] + ' ', first)
        self.assertNotIn(peers[0][0] + ' ', second)
        self.assertIn('翻页', first)
        for width in (24, 32, 84):
            lines = Dashboard(width).connection_screen(frame, 23, 0).splitlines()
            self.assertEqual(len(lines), 23)
            self.assertTrue(all(display_width(line) <= width for line in lines))

    def test_taller_terminals_allocate_the_extra_rows_to_live_data(self):
        with TemporaryDirectory() as directory:
            options = SimpleNamespace(database=Path(directory) / 'missing.db', no_chart=False, width=None, height=None)
            sampler = LiveSampler('en0')
            sampler.samples.append((50, 1, 1000, 300))
            sampler.connections = Connections(4, 2, 0, 0, (('::1', 4),)), (3, 4)
            graph_sizes = []
            for rows in (36, 81):
                out = dashboard_text(options, 'en0', None, (0, 100), True, sampler, os.terminal_size((122, rows)))
                lines = ANSI.sub('', out).splitlines()
                self.assertEqual(len(lines), rows)
                self.assertIn('退出', lines[-1])
                self.assertIn('TCP 连接 · 本机', out)
                self.assertNotIn('独立纵轴', out)
                graph_sizes.append(sum('│' in line[37:] for line in lines))
            self.assertGreater(graph_sizes[1] - graph_sizes[0], 40)

    def test_peak_time_uses_the_individual_bucket_in_the_selected_zone(self):
        values = [0] * 160
        values[19] = 100
        chart = (0, 320, 2, [values, values, values])
        graph = Dashboard(92).graph(chart, 6, ZoneInfo('Asia/Shanghai'), live=True)
        labels = [line for line in graph if '100.00 Mbps' in line]
        self.assertEqual(len(labels), 2)
        for line in labels:
            self.assertIn('08:00:38–08:00:40', line)

    def test_baseline_distinguishes_empty_buckets_from_zero_even_without_color(self):
        for live, values, expected in ((True, [0, 0, None, 0, 0, None, None, None], '┬╴╶┄'),
                                       (False, [0, None, None, 0], '┬┄┄─')):
            chart = (0, len(values), 1, [values] * 3)
            graph = Dashboard(60).graph(chart, 4, None, live=live)
            self.assertIn(expected, next(line for line in graph if '0┼' in line))
            self.assertTrue(any('┄ 缺样' in line for line in graph))

    def test_panel_titles_set_both_foreground_and_background(self):
        for options, code in (({'truecolor': True}, '1;38;2;247;250;255;48;2;9;13;24'),
                              ({'indexed': True}, '1;38;5;231;48;5;233'), ({}, '1;97;40')):
            display = Dashboard(120, color=True, **options)
            for role, title in (('down', 'NETWORK MONITOR'), ('down', '↓ 下载 · 当前'),
                                ('up', '↑ 上传 · 当前'), ('total', '∑ 合计 · 当前')):
                self.assertIn('\033[{}m {} \033[0m'.format(code, title), display.panel(title, [], role=role)[0])

    def test_asymmetric_traffic_uses_each_directions_full_plot_height(self):
        for live, maxima in ((True, ('1e+03', '2.5')), (False, ('1e+03', '2'))):
            chart = (0, 8, 1, [[2] * 8, [800] * 8, [802] * 8])
            lines = Dashboard(80).graph(chart, 4, None, live=live)
            ticks = [line for line in lines if '┤' in line]
            self.assertIn(maxima[0] + '┤', ticks[0])
            self.assertIn(maxima[1] + '┤', ticks[-1])
            self.assertRegex(ticks[-1], r'[\u2801-\u28ff]')

    def test_wave_colors_follow_rates_in_both_directions_even_in_a_one_row_chart(self):
        values = [0.5, None, 4, None, 9, None]
        chart = (0, 6, 1, [values, values, [None] * 6])
        for mode in ({'truecolor': True}, {'indexed': True}, {}):
            with self.subTest(mode=mode):
                display = Dashboard(80, color=True, **mode)
                lines = display.graph(chart, 1, None, live=True)
                traces = [line for line in lines if '┤' in line]
                self.assertEqual(len(traces), 2)
                for role, line in zip(('down', 'up'), traces):
                    inks = display.wave_inks[role]
                    runs = re.findall(r'\x1b\[([0-9;]+)m[\u2801-\u28ff]+', line)
                    self.assertEqual(runs, list(dict.fromkeys(inks[i] for i in (0, 3, 7))))
                    self.assertNotEqual(runs[0], runs[-1])
                    if display.truecolor:
                        rgb = [list(map(int, ink.split(';')[-3:])) for ink in inks]
                        for darker, lighter in zip(rgb, rgb[1:]):
                            self.assertTrue(all(a <= b for a, b in zip(darker, lighter)))
                            self.assertNotEqual(darker, lighter)

    def test_refresh_only_rewrites_changed_rows_and_clears_leftovers(self):
        previous = ['header', 'long reading', 'chart']
        current = ['header', 'reading']
        self.assertEqual(live_update(current, previous, 24),
                         '\033[?2026h\033[2;1Hreading\033[K\033[3;1H\033[J\033[?2026l')
        self.assertEqual(live_update(current, current, 24), '')

    def test_full_height_refresh_leaves_last_row_visible(self):
        self.assertEqual(live_update(['header', 'footer'], [], 2),
                         '\033[?2026h\033[1;1Hheader\033[K\033[2;1Hfooter\033[K\033[?2026l')


class LiveSamplerTests(unittest.TestCase):
    def test_adjust_interval_changes_the_next_deadline_and_uses_actual_elapsed_time(self):
        sampler = LiveSampler('en0', interval=0.3)
        with patch('stats.time.monotonic', side_effect=(10, 10.13, 10.2, 10.34)), \
                patch('stats.monitor.counters', side_effect=((0, 0), (130000, 0), (340000, 0))) as counters:
            sampler.sample()
            for expected in (0.2, 0.1, 0.1):
                self.assertAlmostEqual(sampler.change_interval(-0.1), 10 + expected)
                self.assertEqual(sampler.interval, expected)
            self.assertAlmostEqual(sampler.sample().rates[1], 8)
            self.assertAlmostEqual(sampler.change_interval(0.1), 10.33)
            sampler.sample()
            self.assertAlmostEqual(sampler.sample().rates[1], 8)
        self.assertEqual(counters.call_count, 3)

    def test_rates_use_elapsed_time_and_ignore_early_refreshes(self):
        sampler = LiveSampler('en0')
        with patch('stats.time.monotonic', side_effect=[1.0, 1.5, 3.5, 4.0]), \
                patch('stats.monitor.counters', side_effect=[(100, 200), (1000100, 2500200)]) as counters:
            self.assertIsNone(sampler.sample().rates)
            self.assertIsNone(sampler.sample().rates)
            self.assertEqual(sampler.sample().rates, (8.0, 3.2, 11.2))
            self.assertEqual(sampler.sample().rates, (8.0, 3.2, 11.2))
        self.assertEqual(counters.call_count, 2)
        counters.assert_called_with('en0')

    def test_custom_interval_limits_sampling_between_deadlines(self):
        sampler = LiveSampler('en0', interval=5)
        with patch('stats.time.monotonic', side_effect=[0, 1, 4.9, 5, 5.1]), \
                patch('stats.monitor.counters', side_effect=[(0, 0), (5_000_000, 0)]) as counters:
            for _ in range(5):
                sampler.sample()
        self.assertEqual(counters.call_count, 2)
        self.assertEqual(sampler.reading.rates, (0, 8, 8))
        self.assertEqual(len(sampler.samples), 1)

    def test_live_chart_advances_without_creating_database(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.db'
            with patch('stats.time.time', side_effect=[100, 101, 102]), \
                    patch('stats.time.monotonic', side_effect=[0, 1, 2]), \
                    patch('stats.monitor.counters', side_effect=[(0, 0), (1_000_000, 2_000_000), (4_000_000, 2_000_000)]):
                sampler = LiveSampler('en0')
                sampler.sample()
                self.assertEqual(read_report(path, 'en0', None, 97, 101, 4, sampler).summary.count, 0)
                sampler.sample()
                first = read_report(path, 'en0', None, 97.5, 101.5, 4, sampler)
                sampler.sample()
                second = read_report(path, 'en0', None, 97.5, 102.5, 4, sampler)
            self.assertEqual(first.chart[:3], (97.5, 101.5, 1))
            self.assertEqual(second.chart[:3], (97.5, 102.5, 1.25))
            self.assertEqual(second.chart[3][1], [None, None, 8, 24])
            self.assertEqual(second.summary.totals, [2_000_000, 4_000_000, 6_000_000])
            rolled = read_report(path, 'en0', None, 102, 103, 4, sampler)
            self.assertEqual(rolled.summary.count, 1)
            self.assertEqual(len(sampler.samples), 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_live_session_merges_history_once_and_leaves_persisted_rows_intact(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            writer = storage.Writer(path)
            try:
                writer.write(99, 2, {'en0': (2_000_000, 1_000_000)})
                with patch('stats.time.time', side_effect=[100, 101]), \
                        patch('stats.time.monotonic', side_effect=[1, 2]), \
                        patch('stats.monitor.counters', side_effect=[(0, 0), (2_000_000, 0)]):
                    sampler = LiveSampler('en0')
                    sampler.sample()
                    writer.write(101, 1, {'en0': (500, 250)})
                    sampler.sample()
                report = read_report(path, 'en0', None, 90, 102, 1, sampler)
                self.assertEqual(report.summary.count, 2)
                self.assertEqual(report.summary.duration, 3)
                self.assertEqual(report.chart[3], [[16 / 3], [8], [16 / 3 + 8]])
                persisted = read_report(path, 'en0', None, 90, 102, None)
                self.assertEqual(persisted.summary.totals, [2_000_500, 1_000_250, 3_000_750])
                historical = read_report(path, 'en0', None, 90, 101, 5, sampler)
                self.assertEqual(historical.summary.count, 1)
                self.assertEqual(historical.chart[3][1], [None, None, None, None, 4])
            finally:
                writer.close()

    def test_counter_reset_and_read_failure_restart_the_measurement(self):
        sampler = LiveSampler('en0')
        readings = [(600, 800), (10, 900), FileNotFoundError(), (50, 90), (50, 90)]
        with patch('stats.time.monotonic', side_effect=[1, 3, 5, 7, 9]), \
                patch('stats.monitor.counters', side_effect=readings):
            self.assertIsNone(sampler.sample().rates)
            self.assertIsNone(sampler.sample().rates)
            self.assertEqual(sampler.sample().status, '网卡读数不可用')
            self.assertEqual(sampler.sample().status, '等待采样')
            self.assertEqual(sampler.sample().rates, (0.0, 0.0, 0.0))

    def test_live_chart_rebuckets_entire_range_as_end_advances(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            writer = storage.Writer(path)
            try:
                for timestamp, rate in [(92, 2), (94, 4), (96, 1), (98, 3)]:
                    writer.write(timestamp, 1, {'en0': (0, rate * 125_000)})
            finally:
                writer.close()
            with patch('stats.time.time', return_value=100):
                sampler = LiveSampler('en0')

            def frame(end):
                return read_report(path, 'en0', None, 92, end, 4, sampler)

            first = frame(100)
            self.assertEqual(first.chart[:3], (92, 100, 2))
            self.assertEqual(first.chart[3][1], [2, 4, 1, 3])
            self.assertIn('5┤', '\n'.join(Dashboard(80).graph(first.chart, 4, None, live=True)))
            sampler.samples.append((104, 1, 60_000_000, 12_000_000))
            active = frame(108)
            self.assertEqual(active.chart[:3], (92, 108, 4))
            self.assertEqual(active.chart[3][1], [3, 2, None, 96])
            self.assertEqual(active.summary.count, 5)
            graph = Dashboard(80).graph(active.chart, 4, None, live=True)
            ticks = [line for line in graph if '┤' in line]
            self.assertIn('120┤', ticks[0])   # Download peak is 96 Mbps.
            self.assertIn('600┤', ticks[-1])  # Upload peak is 480 Mbps.

    def test_live_no_chart_retains_only_summary_and_discards_out_of_range_samples(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.db'
            with patch('stats.time.time', return_value=500.5):
                sampler = LiveSampler('en0', keep_chart=False)
                sampler.samples.append((0, 1, 999_999, 999_999))
                for t in range(1, 501):
                    sampler.samples.append((t, 1, t, 500 - t))
                sampler.samples.append((501, 1, 999_999, 999_999))
                report = read_report(path, 'en0', None, 1, 501, None, sampler)
                self.assertEqual(len(sampler.samples), 0)
                self.assertEqual(report.summary.count, 500)
                self.assertEqual(report.summary.duration, 500)
                self.assertEqual(report.summary.totals, [125250, 124750, 250000])
                self.assertEqual(report.summary.minima, [8e-6, 0, 0.004])
                self.assertEqual(report.summary.maxima, [0.004, 0.003992, 0.004])
                refreshed = read_report(path, 'en0', None, 1, 501, None, sampler)
                self.assertEqual(refreshed.summary, report.summary)

    def test_live_command_anchors_last_at_launch_and_caps_explicit_end(self):
        for args, start, later_end in [(('--last', '15m'), 100, 2000),
                                       (('--start', '100', '--end', '1800'), 100, 1800)]:
            with self.subTest(args=args), patch('stats.time.time', return_value=1000) as clock, \
                    patch('stats.sys.stdin.isatty', return_value=True), \
                    patch('stats.sys.stdout.isatty', return_value=True), \
                    patch.dict(os.environ, {'TERM': 'xterm'}), patch('stats.LiveSampler.sample') as sample, \
                    patch('stats.dashboard_text', return_value='frame') as render, \
                    patch('stats.run_live') as live:
                def run_frames(render_frame, changed, change_interval):
                    render_frame(os.terminal_size((100, 30)), True)
                    clock.return_value = 2000
                    render_frame(os.terminal_size((90, 30)), False)
                    render_frame(os.terminal_size((90, 30)), True)

                live.side_effect = run_frames
                self.assertEqual(network_monitor.main(['stats', '--live', '-i', 'en0', *args]), 0)
                ranges = [call.args[3] for call in render.call_args_list]
                self.assertEqual([left for left, _ in ranges], [start, start, start])
                self.assertAlmostEqual(ranges[0][1], 1000)
                self.assertEqual(ranges[1], ranges[0])
                self.assertAlmostEqual(ranges[2][1], later_end)
                self.assertEqual(sample.call_count, 2)

    def test_default_entry_uses_live_stats_and_its_configuration(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            config.write_text('[monitor]\ndatabase = history.db\ninterval = 5\n[stats]\ntimezone = UTC\n')
            for arguments, database, timezone in (
                    ([], 'history.db', 'UTC'),
                    (['-d', 'selected.db', '--tz', '+08:00'], 'selected.db', '+08:00')):
                with self.subTest(arguments=arguments), patch('configuration.search_paths', return_value=[
                        config, Path(directory) / 'system.ini']), \
                        patch('stats.run') as run:
                    for prefix in ([], ['stats', '--live']):
                        self.assertEqual(network_monitor.main(prefix + arguments), 0)
                        args, _ = run.call_args.args
                        self.assertTrue(args.live)
                        self.assertFalse(args.plain)
                        self.assertEqual((args.database, args.timezone, args.interval), (database, timezone, None))

    def test_live_graph_packs_independent_samples_and_preserves_gaps(self):
        samples = [0, 0.75, None, 0.25, 0.25]
        self.assertEqual(area_cells(samples, 1, 1, above=True, smooth=False), [['\u28f0', '\u2880', '\u2840']])
        self.assertEqual(area_cells(samples, 1, 1, above=False, smooth=False), [['\u2839', '\u2808', '\u2801']])

    def test_live_dashboard_borders_have_one_column_margins(self):
        with TemporaryDirectory() as directory, patch('stats.time.time', return_value=100.25):
            options = SimpleNamespace(database=Path(directory) / 'missing.db', no_chart=False, width=None, height=4)
            for width in (37, 80, 81, 200, 241, 400):
                size = os.terminal_size((width, 30))
                with self.subTest(width=width), patch('stats.os.get_terminal_size', return_value=size):
                    sampler = LiveSampler('en0')
                    sampler.samples.append((100.1, 1.2, 2, 3))
                    output = dashboard_text(options, 'en0', None, (0, 86400), True, sampler)
                    self.assertIn('网速趋势', output)
                    axis = next(line for line in ANSI.sub('', output).splitlines() if '0┼' in line)
                    plot = axis.split('┼')[1].split('│')[0].strip()
                    self.assertEqual(len(plot), width - 14)
                    for line in ANSI.sub('', output).splitlines():
                        if line.lstrip().startswith(('┏', '│', '└')):
                            left = len(line) - len(line.lstrip())
                            self.assertEqual(left, 1, line)
                            self.assertEqual(width - display_width(line), 1, line)

    def test_wide_layout_buckets_match_the_visible_plot_after_resize(self):
        with TemporaryDirectory() as directory:
            options = SimpleNamespace(database=Path(directory) / 'missing.db', no_chart=False, width=None, height=None)
            sampler = LiveSampler('en0')
            sampler.started_at = 0
            sampler.samples.append((50, 1, 1000, 300))
            # Resize the same session and then request explicit chart dimensions.
            for width, height, requested, chart_height, plot, split in (
                    (122, 36, None, None, 71, True), (62, 26, None, None, 48, False),
                    (122, 24, None, None, 108, False), (122, 36, 88, None, 88, False),
                    (122, 36, 20, None, 20, True), (122, 36, None, 4, 108, False)):
                with self.subTest(size=(width, height), requested=requested, chart_height=chart_height):
                    options.width, options.height = requested, chart_height
                    with patch('stats.read_report', wraps=read_report) as query:
                        raw = dashboard_text(options, 'en0', None, (0, 100), True, sampler,
                                             os.terminal_size((width, height)))
                    self.assertEqual(query.call_args.args[3:6], (0, 100, 2 * plot))
                    lines = ANSI.sub('', raw).splitlines()
                    self.assertLessEqual(len(lines), height)
                    self.assertTrue(all(display_width(line) <= width - 1 for line in lines))
                    title = next(line for line in lines if '网速趋势' in line)
                    self.assertEqual('↓ 下载' in title, split)
