import fcntl
from io import StringIO
import os
from pathlib import Path
import re
import signal
import struct
import sys
from tempfile import TemporaryDirectory
import termios
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import network_monitor
from ip_traffic import PeerTraffic
from stats import LiveSampler, dashboard_text, live_input, live_update, read_report, run_live, terminal_theme_hint
from stats_display import ANSI, Dashboard, LiveSample, Report, area_cells, connection_columns, display_width
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
    def test_six_local_ips_fit_on_one_page_and_keep_stable_positions(self):
        addresses = ['192.0.2.2', '192.0.2.4', '192.0.2.8', '192.0.2.10', '192.0.2.11',
                     '2001:db8:1234:5678:9abc:def0:1234:5678']
        capture = SimpleNamespace(interface='eth0', status='', ready=True,
                                  local_rows=[PeerTraffic(ip, 500, 0, n, 0) for n, ip in enumerate(addresses)])
        for width, rows in ((80, 24), (60, 18)):
            display = Dashboard(width)
            first = display.ip_screen(capture, rows, 0, view='ip-local')
            capture.local_rows.reverse()
            self.assertEqual(display.ip_screen(capture, rows, 0, view='ip-local'), first)
            self.assertEqual(display.paging, (0, 1))
            self.assertEqual([ip for ip in addresses[:-1]], sorted(addresses[:-1], key=first.index))
            self.assertIn('2001:db8:', first)
            self.assertTrue(all(display_width(line) <= width for line in first.splitlines()))

    def test_tcp_address_shortcuts_and_clicks_keep_the_selected_identity(self):
        addresses = [('tcp-address', None, '192.0.2.2'), ('tcp-address', (1, 99), '192.0.2.2')]
        def frame(size, collect, view, page, sort, theme, selected):
            targets = [(1, 0, 10, addresses[0]), (2, 0, 10, addresses[1])]
            return '\n' * 23, float('inf'), targets if not selected else [], (page, 2)
        render = Mock(side_effect=frame)
        with patch('stats.sys.stdin.fileno', return_value=1), patch('stats.sys.stdout', StringIO()), \
                patch('stats.enter_cbreak'), patch('stats.terminal_size', return_value=os.terminal_size((80, 24))), \
                patch('select.select', return_value=([1], [], [])), \
                patch('stats.os.read', side_effect=[b'c', b'n', b'2', b'n', b'b', b'\x1b[<0;1;2M', b'q']):
            run_live(render, Event(), Mock())
        self.assertEqual([(c.args[3], c.args[6]) for c in render.call_args_list],
                         [(0, None), (0, None), (1, None), (0, addresses[1][1:]), (1, addresses[1][1:]),
                          (1, None), (0, addresses[0][1:])])

    def test_ip_readouts_share_and_sort_targets_follow_the_selected_mode(self):
        capture = SimpleNamespace(interface='en0', status='', ready=True,
                                  rows=[PeerTraffic('198.51.100.1', 9000, 3000, 4, 2)],
                                  local_rows=[PeerTraffic('192.0.2.1', 3000, 2000, 2, 1),
                                              PeerTraffic('2001:db8::1', 6000, 1000, 0, .001)])
        for view, reading in (('ip', '4.00 Mbps'), ('ip-local', '2.00 Mbps')):
            with self.subTest(view=view):
                targets = []
                lines = Dashboard(100).ip_screen(capture, 28, 0, ('up_mbps', True), targets,
                                                  view=view).splitlines()
                self.assertIn(reading, '\n'.join(lines[:6]))
                row, left, right, _ = next(t for t in targets if t[3] == 'up_mbps')
                cells = ''.join(c.ljust(display_width(c)) for c in lines[row])
                self.assertIn('▾', cells[left:right])
                self.assertEqual('双向速率对比' in '\n'.join(lines), view == 'ip-local')
                if view == 'ip-local':
                    self.assertIn('<1%', '\n'.join(lines))

    def test_ip_headings_cover_all_columns_and_rank_before_paging(self):
        peers = [PeerTraffic('192.0.2.10', 100, 400, 2, 9), PeerTraffic('192.0.2.2', 200, 300, 4, 3)]
        capture = SimpleNamespace(interface='en0', status='', rows=peers)
        expected = {'address': peers[0], 'download': peers[1], 'upload': peers[0],
                    'down_mbps': peers[1], 'up_mbps': peers[0]}
        for width in (120, 58, 18):
            for column, first in expected.items():
                for descending in (True, False):
                    with self.subTest(width=width, column=column, descending=descending):
                        headings = []
                        screen = Dashboard(width).ip_screen(capture, 6, 0, (column, descending), headings)
                        headings = [heading for heading in headings if heading[0] < 5 and heading[3] not in 'pn']
                        self.assertEqual({name for _, _, _, name in headings}, set(expected))
                        row, left, right, _ = next(heading for heading in headings if heading[3] == column)
                        self.assertLess(left, right)
                        self.assertIn(' ▾' if descending else ' ▴', screen.splitlines()[row])
                        self.assertEqual(screen.count('▾') + screen.count('▴'), 1)
                        self.assertTrue(all(display_width(line) <= width for line in screen.splitlines()))
                        second = next(peer for peer in peers if peer != first)
                        self.assertIn((first if descending else second).address, screen.splitlines()[max(h[0] for h in headings) + 1])

    def test_live_home_places_ready_ip_table_below_the_chart(self):
        peers = [PeerTraffic('192.0.2.{}'.format(i), 100, 200, i, 2) for i in range(1, 24)]
        peers.append(PeerTraffic('223.5.5.5', 100, 200, 24, 2))
        capture = SimpleNamespace(interface='en0', status='', ready=True, rows=peers,
                                  hostname=lambda address: 'ptr' if address == '223.5.5.5' else '',
                                  peer_details=lambda address: ('杭州·浙江·中国', 'Alibaba US Technology Co., Ltd.', 'AS45102')
                                  if address == '223.5.5.5' else ('', '', ''))
        options = SimpleNamespace(database=None, no_chart=False, width=None, height=None)
        sampler = LiveSampler('en0')

        def query(_db, interface, zone, start, end, buckets, _sampler):
            chart = (start, end, 1, [[0.1] * buckets] * 3) if buckets else None
            return Report(interface, zone, start, end, storage.Summary(), chart)

        with patch.dict(os.environ, NO_COLOR='1'):
            for size in ((100, 40), (120, 42), (120, 30)):
                for ready, status in ((True, ''), (False, ''), (False, '当前用户没有此网卡的抓包权限')):
                    with self.subTest(size=size, ready=ready, status=status):
                        capture.ready, capture.status = ready, status
                        targets = []
                        screen = dashboard_text(options, 'en0', None, (0, 10), True, sampler,
                                                os.terminal_size(size), query=query, ip_capture=capture,
                                                ip_sort=('down_mbps', True), click_targets=targets)
                        lines = screen.splitlines()
                        self.assertEqual(len(lines), size[1])
                        self.assertTrue(all(display_width(line) <= size[0] for line in lines))
                        if not ready or size[1] < 36:
                            self.assertNotIn('IP 流量 · en0', screen)
                            continue
                        chart = next(i for i, line in enumerate(lines) if '网速趋势' in line)
                        table = next(i for i, line in enumerate(lines) if 'IP 流量 · en0' in line)
                        self.assertGreater(table, chart)
                        chart_left = display_width(lines[chart][:lines[chart].index('网速趋势') - 3])
                        table_left = display_width(lines[table][:lines[table].index('IP 流量') - 3])
                        self.assertEqual(table_left, chart_left)
                        self.assertIn('223.5.5.5', lines[table + 2])
                        self.assertIn('杭州·浙江·中国  阿里云  AS45102  ptr', lines[table + 3])
                        self.assertIn('↓ 下载速率 ▾', lines[table + 1])
                        self.assertEqual({key for row, _, _, key in targets if row == table + 1},
                                         {'address', 'down_mbps', 'up_mbps', 'download', 'upload'})
                        pager = next(i for i, line in enumerate(lines) if '上页' in line and '下页' in line)
                        self.assertGreater(pager, table + 1)
                        self.assertLess(pager, len(lines) - 1)
                        self.assertRegex(lines[pager], r'上页 +1/\d+ +n 下页')
                        self.assertNotRegex(lines[table], r' \d+/\d+ ')
                        self.assertEqual({key for row, _, _, key in targets if row == pager}, {'n'})
                        self.assertNotIn('下页', lines[-1])

    def test_live_mouse_input_handles_fragmented_clicks_without_emitting_keys(self):
        events, pending = live_input(b'i\x1b[<0;120;')
        self.assertEqual(events, ['i'])
        events, pending = live_input(pending + b'2M\x1b[<0;120;2m\x1b[3~n')
        self.assertEqual((events, pending), ([(119, 1), 'n'], b''))

    @unittest.skipUnless(sys.platform == 'linux' and os.uname().machine in ('x86_64', 'aarch64'),
                         'requires Linux termios2 on x86-64 or arm64')
    def test_live_sets_cbreak_without_libc_and_preserves_custom_baud(self):
        output = StringIO()
        previous_handler = signal.getsignal(signal.SIGTERM)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tcgets2, tcsets2 = 0x802c542a, 0x402c542b
        layout = struct.Struct('=IIIIB19sII')
        attributes = list(layout.unpack(fcntl.ioctl(slave, tcgets2, bytes(layout.size))))
        cbaud, bother = 0x100f, 0x1000
        attributes[2] = (attributes[2] & ~cbaud) | bother
        attributes[-2:] = [14400, 14400]
        fcntl.ioctl(slave, tcsets2, layout.pack(*attributes))
        snapshot = fcntl.ioctl(slave, tcgets2, bytes(layout.size))

        def frame(*_):
            current = layout.unpack(fcntl.ioctl(slave, tcgets2, bytes(layout.size)))
            self.assertFalse(current[3] & (termios.ICANON | termios.ECHO))
            self.assertEqual(current[-2:], (14400, 14400))
            return 'frame', float('inf'), [(0, 0, 1, 'c')], (0, 1)

        render = Mock(side_effect=frame)
        with patch('stats.sys.stdin.fileno', return_value=slave), patch('stats.sys.stdout', output), \
                patch('stats.terminal_size', return_value=os.terminal_size((80, 24))), \
                patch('tty.setcbreak') as cbreak, \
                patch('select.select', return_value=([slave], [], [])), \
                patch('stats.os.read', side_effect=[b'\x1b[<0;1;1M', b'c', b'q']):
            run_live(render, Event(), Mock())
        cbreak.assert_not_called()
        self.assertEqual(fcntl.ioctl(slave, tcgets2, bytes(layout.size)), snapshot)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertEqual([call.args[2] for call in render.call_args_list], ['traffic', 'connections', 'traffic'])
        self.assertEqual(render.call_args.args[0].lines, 24)
        self.assertIn('\033[?1000h', output.getvalue())
        self.assertIn('\033[?1000l', output.getvalue())
        self.assertTrue(output.getvalue().endswith('\033[?25h\033[?1049l'))

    def test_terminal_background_reply_can_be_fragmented_and_mixed_with_input(self):
        events, pending = live_input(b'\x1b]11;rgb:ffff/ff')
        self.assertEqual(events, [])
        events, pending = live_input(pending + b'ff/ffff\x1b\\q\x1b]11;rgb:1a/1b/1c\a')
        self.assertEqual((events, pending), (['light', 'q', 'dark'], b''))
        for hint, expected in (('15;0', 'dark'), ('0;15', 'light'), ('15;232', 'dark'), ('invalid', None)):
            with self.subTest(hint=hint), patch.dict(os.environ, COLORFGBG=hint):
                self.assertEqual(terminal_theme_hint(), expected)

    def test_live_footer_click_targets_match_each_view(self):
        peers = tuple(('192.0.2.{}'.format(i), 1) for i in range(1, 20))
        capture = SimpleNamespace(interface='en0', status='', ready=True, rows=[PeerTraffic(addr, 100, 200, 1, 2)
                                                                    for addr, _ in peers],
                                  local_rows=[PeerTraffic('192.0.2.100', 0, 0, 0, 0)])
        sampler = LiveSampler('en0')
        sampler.connections = Connections(19, peers=peers), ()
        options = SimpleNamespace(database=None, no_chart=True, width=None, height=None)
        report = Report('en0', None, 0, 10, storage.Summary())
        for width in (20, 44, 120):
            for view in ('traffic', 'connections', 'ip', 'ip-local'):
                with self.subTest(width=width, view=view), patch.dict(os.environ, NO_COLOR='1'):
                    targets = []
                    screen = dashboard_text(options, 'en0', None, (0, 10), True, sampler,
                                            os.terminal_size((width, 17)), query=lambda *_: report,
                                            view=view, ip_capture=capture, click_targets=targets)
                    footer = ''.join(char.ljust(display_width(char)) for char in screen.splitlines()[-1])
                    buttons = {key: (left, right) for row, left, right, key in targets if row == 16}
                    expected = set('qci+-')
                    if view in ('ip', 'ip-local'):
                        expected.add('m')
                    if view == 'connections' and width >= 44:
                        expected.add('n')
                    self.assertEqual(expected, buttons.keys())
                    for key, (left, right) in buttons.items():
                        self.assertGreaterEqual(left, 1)
                        self.assertLessEqual(right, width - 1)
                        self.assertTrue(footer[left:right].lstrip().startswith(key))
                    minus_left, minus_right = buttons['-']
                    plus_left, plus_right = buttons['+']
                    self.assertEqual(footer[minus_right:plus_left], ' 1s ')
                    self.assertIn('-', footer[minus_left:minus_right])
                    self.assertIn('+', footer[plus_left:plus_right])
                    self.assertLessEqual(max(minus_right - minus_left, plus_right - plus_left),
                                         min(buttons[key][1] - buttons[key][0] for key in 'qci'))
                    if width == 120:
                        self.assertEqual((minus_right - minus_left, plus_right - plus_left), (3, 3))

    def test_ip_mode_can_switch_by_mouse_and_keyboard(self):
        render = Mock(return_value=('\n' * 23, float('inf'), [(23, 0, 1, 'm')], (0, 1)))
        with patch('stats.sys.stdin.fileno', return_value=1), patch('stats.sys.stdout', StringIO()), \
                patch('stats.enter_cbreak'), patch('stats.terminal_size', return_value=os.terminal_size((80, 24))), \
                patch('select.select', return_value=([1], [], [])), \
                patch('stats.os.read', side_effect=[b'i', b'\x1b[<0;1;24M', b'i', b'i', b'm', b'q']):
            run_live(render, Event(), Mock())
        self.assertEqual([call.args[2] for call in render.call_args_list],
                         ['traffic', 'ip', 'ip-local', 'traffic', 'ip-local', 'ip'])

    def test_peer_paging_clamps_and_disables_boundary_buttons(self):
        peers = tuple(('192.0.2.{}'.format(i), 1) for i in range(1, 41))
        capture = SimpleNamespace(interface='en0', status='',
                                  rows=[PeerTraffic(addr, 100, 200, 1, 2) for addr, _ in peers])
        connections = Connections(40, peers=peers), ()
        display = Dashboard(120, color=True)
        views = ((display.ip_screen, capture), (display.connection_screen, connections))
        for render, data in views:
            with self.subTest(view=render.__name__):
                control_row = 15 if render == display.ip_screen else 16
                for page, expected, disabled in ((-1, {'n'}, ' p 上页 '), (1, {'n', 'p'}, None),
                                                (999, {'p'}, ' n 下页 ')):
                    targets = []
                    screen = render(data, 17, page, click_targets=targets)
                    current, total = display.paging
                    self.assertEqual(current, min(max(0, page), total - 1))
                    self.assertEqual(screen, render(data, 17, current))
                    self.assertEqual({key for row, _, _, key in targets if row == control_row and key in 'pn'}, expected)
                    lines = screen.splitlines()
                    controls = lines[control_row]
                    self.assertLess(controls.index('上页'), controls.index('下页'))
                    if render == display.ip_screen:
                        plain = ANSI.sub('', controls)
                        self.assertTrue(plain.startswith('└'))
                        self.assertRegex(plain, r'上页 +{}/{} +n 下页'.format(current + 1, total))
                        self.assertNotRegex(lines[0], r' \d+/\d+ ')
                        for row, left, right, key in targets:
                            if row == control_row and key in 'pn':
                                button = ' p 上页 ' if key == 'p' else ' n 下页 '
                                start = display_width(plain[:plain.index(button)])
                                self.assertEqual((left, right), (start, start + display_width(button)))
                        self.assertNotIn('下页', lines[-1])
                        self.assertNotIn('上页', lines[-1])
                    if disabled:
                        self.assertIn('\033[2;39m' + disabled + '\033[0m', controls)

    def test_connection_page_draws_a_thin_line_and_scrolls_from_the_right(self):
        display = Dashboard(150)

        def plot(history):
            screen = display.connection_screen((Connections(8), tuple(enumerate(history))), 30, 0)
            traces = [line.split('│')[2][:-1] for line in screen.splitlines() if line.count('│') == 3]
            return [''.join(char if '\u2801' <= char <= '\u28ff' else ' '
                            for char in ''.join(char.ljust(display_width(char)) for char in trace)) for trace in traces]

        first = plot((8,))
        self.assertEqual(sum(char != ' ' for row in first for char in row), 1)
        self.assertTrue(any(row[-1] != ' ' for row in first))
        self.assertFalse(first[0].strip())

        flat = plot((8, 8, 8))
        self.assertEqual([row.rstrip()[-3:] for row in flat if row.strip()], ['⠐⠒⠒'])

        gap = plot((8, None, 2))
        self.assertTrue(all(row[-2] == ' ' for row in gap))
        self.assertEqual(sum(row[-3] != ' ' for row in gap), 1)
        self.assertEqual(sum(row[-1] != ' ' for row in gap), 1)
        self.assertTrue(any(row[-1] != ' ' for row in plot((8, 0))[1:-1]))

        compact = display.connection_trend(((0, 8), (1, None), (2, 0)), 34, 1)[0]
        self.assertTrue(compact.endswith('⠐ ⢀'))
        self.assertIn('峰值 8', compact)

    def test_connection_trend_compresses_elapsed_time_and_keeps_the_peak(self):
        history = ((0, 2), (1, 34), (2, 3), (31, None), (40, 5), (100, 7), (101, 1))
        self.assertEqual(connection_columns(history[:3], 4), [None, *history[:3]])
        self.assertEqual(connection_columns(history, 5), [(1, 34), (40, 5), None, (100, 7), (101, 1)])
        long_history = (*history, *((102 + i, 2) for i in range(140)))
        display = Dashboard(80, zone=ZoneInfo('Asia/Shanghai'))
        screen = display.connection_screen((Connections(2), long_history), 30, 0)
        self.assertIn('147 次', screen)
        self.assertIn('43│', screen)
        self.assertIn('峰值 34 · 08:00:01', screen)

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
            (Connections(2, peers=(('::1', 2),)), ((0, 2),)),
            (Connections(12, peers=(('::1', 9), ('192.0.2.1', 3))), ((0, 2), (1, 12))),
            (Connections(status='连接表不可用'), ((0, 2), (1, 12), (2, None))),
            (Connections(), ((0, 2), (1, 12), (2, None), (3, 0))),
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

    def test_wide_traffic_header_fills_existing_space_with_range_and_sample_details(self):
        summary = storage.Summary()
        summary.add_sample(60, 1, 1000, 2000)
        summary.add_sample(90, 1, 3000, 4000)
        report = Report('lo', ZoneInfo('UTC'), 0, 100, summary)
        display = Dashboard(118)
        live = LiveSample((1, 2, 3))
        tall = display.render(report, None, live, rows=42).splitlines()
        self.assertEqual(len(tall), 42)
        self.assertTrue(all(display_width(line) <= 118 for line in tall))
        header = '\n'.join(tall[:7])
        for text in ('█▄  █', 'UTC', '累计流量', '10.00 KB', '6.00 KB', '4.00 KB', '↓ 60%', '↑ 40%',
                     '查询时长', '首条采样', '00:01:00', '末条采样', '00:01:30'):
            self.assertIn(text, header)
        ultrawide = Dashboard(240).render(report, None, live, rows=42).splitlines()
        for screen in (tall, ultrawide):
            self.assertTrue(screen[2].startswith('│ █▄  █'))
            self.assertIn('│ 累计流量', screen[1])
            self.assertIn('10.00 KB │ 采样', screen[1])
            self.assertTrue(screen[1].endswith('2 次 │'))
            self.assertRegex(screen[3], r'\+0000 +│ ↑ 上传')
        wide = Dashboard(120).render(report, None, live, rows=42).splitlines()
        for wide_line, ultrawide_line in zip(wide[1:6], ultrawide[1:6]):
            _, wide_context, wide_readings = wide_line.split('│', 2)
            _, ultrawide_context, ultrawide_readings = ultrawide_line.split('│', 2)
            self.assertEqual(wide_context.rstrip(), ultrawide_context.rstrip())
            self.assertEqual(display_width(ultrawide_context) - display_width(wide_context), 120)
            self.assertEqual(wide_readings, ultrawide_readings)

        startup = Report('lo', report.zone, 0, 0, storage.Summary())
        screen = display.render(startup, None, LiveSample(), rows=42).splitlines()
        self.assertEqual(screen[6], tall[6])
        self.assertRegex(screen[4], r'首条采样 [ ┄]*—')
        shorter = display.render(report, None, live, rows=38).splitlines()
        self.assertIn('累计流量  10.00 KB', '\n'.join(shorter[:5]))

    def test_wide_header_leaders_stay_fixed_as_values_grow(self):
        summary = storage.Summary()
        report = Report('lo', ZoneInfo('UTC'), 0, 100, summary)
        display = Dashboard(128)

        def leaders():
            lines = display.render(report, None, LiveSample(), rows=42).splitlines()
            return [(i, display_width(line[:match.start()]), len(match[0]))
                    for i, line in enumerate(lines[1:6]) for match in re.finditer(r'┄+', line)]

        initial = leaders()
        self.assertEqual(len(initial), 8)
        self.assertEqual(len({(column, length) for _, column, length in initial}), 2)
        for timestamp in range(1, 10):
            summary.add_sample(timestamp, 1, 1000, 2000)
        self.assertEqual(leaders(), initial)
        summary.add_sample(10, 10, 100_000_000, 200_000_000)
        self.assertEqual(leaders(), initial)

    def test_connection_pages_fit_and_reveal_the_remaining_ipv6_peers(self):
        peers = tuple(('2001:db8:ffff:ffff:ffff:ffff:ffff:{:x}'.format(i), 20 - i) for i in range(20))
        frame = Connections(210, 4, 2, 0, peers), ((0, 10), (1, None), (2, 210))
        display = Dashboard(84)
        first = display.connection_screen(frame, 17, 0)
        second = display.connection_screen(frame, 17, 1)
        self.assertIn(peers[0][0] + ' ', first)
        self.assertNotIn(peers[0][0] + ' ', second)
        self.assertIn('下页', first)
        self.assertIn('上页', first)
        for width in (24, 32, 84):
            lines = Dashboard(width).connection_screen(frame, 23, 0).splitlines()
            self.assertEqual(len(lines), 23)
            self.assertTrue(all(display_width(line) <= width for line in lines))

    def test_taller_terminals_allocate_the_extra_rows_to_live_data(self):
        with TemporaryDirectory() as directory:
            options = SimpleNamespace(database=Path(directory) / 'missing.db', no_chart=False, width=None, height=None)
            sampler = LiveSampler('en0')
            sampler.samples.append((50, 1, 1000, 300))
            sampler.connections = Connections(4, 2, 0, 0, (('::1', 4),)), ((0, 3), (1, 4))
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

    def test_dashboard_themes_keep_background_except_on_footer_buttons(self):
        options = SimpleNamespace(database=None, width=None, height=None, no_chart=True)
        summary = storage.Summary()
        summary.add_sample(10, 1, 1000, 2000)
        report = Report('en0', None, 0, 10, summary)
        sampler = LiveSampler('en0')
        size = os.terminal_size((120, 30))
        modes = (
            ({'COLORTERM': 'truecolor'}, ('38;2;0;87;117', '1;38;2;79;248;238', '38;2;0;135;140')),
            ({'TERM': 'xterm-256color'}, ('38;5;24', '1;36', '38;5;30')),
            ({'TERM': 'xterm'}, ('1;36', '1;36', '1;36')),
        )
        for env, foregrounds in modes:
            for live in (None, sampler):
                with self.subTest(env=env, live=live is not None), patch.dict(os.environ, env, clear=True):
                    for theme, foreground in zip(('light', 'dark', None), foregrounds):
                        screen = dashboard_text(options, 'en0', None, (0, 10), True, live, size,
                                                query=lambda *_: report, theme=theme)
                        body = screen
                        if live:
                            body, footer = screen.rsplit('\n', 1)
                            self.assertRegex(footer, r';7m q 退出 \x1b\[0m')
                            self.assertRegex(footer, r';7m - \x1b\[0m .*?1s.*? \x1b\[[0-9;]+;7m \+ ')
                        styles = re.sub(r'38;(?:2;\d+;\d+;\d+|5;\d+)', '39', body)
                        self.assertNotRegex(styles, r'\x1b\[(?:[0-9]+;)*(?:7|4[0-8]|10[0-7])(?:;|m)')
                        self.assertIn('\033[{}m┏━'.format(foreground), screen)
                        if env.get('TERM') == 'xterm':
                            self.assertIn('\033[1;35m┏━', screen)
                            self.assertIn('\033[1;33m┏━', screen)
                        if theme != 'dark':
                            self.assertIn('\033[1;39m NETWORK MONITOR \033[0m', screen)
        with patch.dict(os.environ, {'NO_COLOR': '1', 'COLORTERM': 'truecolor'}, clear=True):
            screen = dashboard_text(options, 'en0', None, (0, 10), True, sampler, size,
                                    query=lambda *_: report)
            self.assertNotIn('\033', screen)

    def test_large_readings_use_original_block_glyphs_on_all_terminals(self):
        report = Report('en0', None, 0, 10, storage.Summary())
        sampler = LiveSampler('en0')
        sampler.reading = LiveSample((1, 2, 3))
        options = SimpleNamespace(database=None, no_chart=True, width=None, height=None)
        for env in ({}, {'TERM_PROGRAM': 'Apple_Terminal'}, {'TERM_PROGRAM': 'iTerm.app'}):
            for width in (80, 120):
                with self.subTest(env=env, width=width), patch.dict(os.environ, {'TERM': 'xterm-256color', **env},
                                                                  clear=True):
                    screen = dashboard_text(options, 'en0', None, (0, 10), True, sampler,
                                            os.terminal_size((width, 30)), query=lambda *_: report)
                    lines = ANSI.sub('', screen).splitlines()
                    self.assertIn('█▄  █ █▄ ▄█', lines[1])
                    card = next(i for i, line in enumerate(lines) if '↓ 下载 · 当前' in line)
                    self.assertIn('▀▀█', lines[card + 1])
                    self.assertIn('█▀▀', lines[card + 2])
                    self.assertIn('▀▀▀ Mbps', lines[card + 3])

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
        for mode in ({'truecolor': True, 'theme': 'dark'}, {'indexed': True, 'theme': 'dark'},
                     *({'theme': theme} for theme in ('dark', 'light', None))):
            with self.subTest(mode=mode):
                display = Dashboard(80, color=True, **mode)
                lines = display.graph(chart, 1, None, live=True)
                traces = [line for line in lines if '┤' in line]
                self.assertEqual(len(traces), 2)
                self.assertTrue(set(display.wave_inks['down']).isdisjoint(display.wave_inks['up']))
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
                         '\033[?2026h\033[2;1H\033[Kreading\033[3;1H\033[J\033[?2026l')
        self.assertEqual(live_update(current, current, 24), '')

    def test_full_height_refresh_leaves_last_row_visible(self):
        self.assertEqual(live_update(['header', 'footer'], [], 2),
                         '\033[?2026h\033[1;1H\033[Kheader\033[2;1H\033[Kfooter\033[?2026l')


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
                    patch('stats.run_live') as live, patch('tcp_connections.ConnectionReader.refresh'):
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

    def test_live_command_starts_one_capture_when_resized_to_show_home_ip(self):
        with patch('stats.sys.stdin.isatty', return_value=True), patch('stats.sys.stdout.isatty', return_value=True), \
                patch.dict(os.environ, {'TERM': 'xterm'}), patch('stats.LiveSampler.sample'), \
                patch('stats.dashboard_text', return_value='frame') as display, patch('stats.run_live') as live, \
                patch('ip_traffic.IPTraffic') as capture, patch('tcp_connections.ConnectionReader.refresh'):
            def run_frames(render_frame, changed, _change_interval):
                render_frame(os.terminal_size((80, 24)), True)
                capture.assert_not_called()
                render_frame(os.terminal_size((120, 42)), False)
                capture.assert_called_once_with('en0', changed)
                render_frame(os.terminal_size((120, 42)), True, 'ip')
                render_frame(os.terminal_size((80, 24)), True)

            live.side_effect = run_frames
            self.assertEqual(network_monitor.main(['stats', '--live', '-i', 'en0']), 0)
            capture.assert_called_once()
            capture.return_value.start.assert_called_once()
            capture.return_value.close.assert_called_once()
            self.assertEqual([call.kwargs['ip_capture'] for call in display.call_args_list],
                             [None, capture.return_value, capture.return_value, capture.return_value])

    def test_default_entry_uses_live_stats_and_its_configuration(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            config.write_text('[monitor]\ndatabase = history.db\ninterval = 5\n[stats]\ntimezone = UTC\n')
            for arguments, database, timezone, day in (
                    ([], 'history.db', 'UTC', None),
                    (['--database', 'selected.db', '--tz', '+08:00', '-d', '2026-11-19'],
                     'selected.db', '+08:00', '2026-11-19')):
                with self.subTest(arguments=arguments), patch('configuration.search_paths', return_value=[
                        config, Path(directory) / 'system.ini']), \
                        patch('stats.run') as run:
                    for prefix in ([], ['stats', '--live']):
                        self.assertEqual(network_monitor.main(prefix + arguments), 0)
                        args, _ = run.call_args.args
                        self.assertTrue(args.live)
                        self.assertFalse(args.plain)
                        self.assertEqual((args.database, args.timezone, args.interval), (database, timezone, None))
                        self.assertEqual(args.day, day)

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
                            self.assertEqual(width - display_width(line.rstrip()), 1, line)

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
                    self.assertTrue(all(display_width(line) <= width for line in lines))
                    title = next(line for line in lines if '网速趋势' in line)
                    self.assertEqual('↓ 下载' in title, split)
