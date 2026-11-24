#!/usr/bin/env python3
"""Query daily SQLite partitions and display statistics in the selected zone."""
from collections import deque
from contextlib import ExitStack, suppress
import errno
import math
import os
import re
import shutil
import subprocess
import sys
import time

import monitor
from stats_display import (Dashboard, IP_VIEWS, LiveSample, Report, chart_scale, display_width, ip_preview_budget,
                           ip_preview_fits, metric_rail_width, pad, SINGLE_PAGE, traffic)
import storage
import time_ranges


REFRESH_SECONDS = 1.0
MIN_LIVE_INTERVAL = 0.1
RESIZE_POLL_SECONDS = 0.04
RESIZE_DEBOUNCE_SECONDS = 0.075
RESIZE_MAX_DELAY_SECONDS = 0.2
ESCAPE_INPUT = re.compile(rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|O[@-~]|[^\[\]O\x1b])')
BACKGROUND_QUERY = '\033]11;?\033\\'
BACKGROUND_REPLY = re.compile(rb'\x1b\]11;rgb:([0-9a-f]{1,4})/([0-9a-f]{1,4})/([0-9a-f]{1,4})'
                              rb'(?:\x07|\x1b\\)', re.IGNORECASE)
MOUSE_PRESS = re.compile(rb'\x1b\[<0;([0-9]{1,6});([0-9]{1,6})M')
ENABLE_MOUSE = '\033[?1006h\033[?1000h'
DISABLE_MOUSE = '\033[?1000l\033[?1006l'
TRAFFIC_PERIODS = {
    'day': (15, '天', '%Y-%m-%d'), 'hour': (24, '小时', '%Y-%m-%d %H:%M %z'),
    'minute': (60, '分钟', '%Y-%m-%d %H:%M %z'), 'second': (60, '秒', '%Y-%m-%d %H:%M:%S %z'),
    'month': (12, '月', '%Y-%m'),
}


class LiveSampler:
    def __init__(self, interface, interval=REFRESH_SECONDS, keep_chart=True):
        self.interface = interface
        self.interval = interval
        self.started_at = time.time()
        self.next_sample = -math.inf
        self.last_poll = -math.inf
        self.samples = deque()
        self.keep_chart = keep_chart
        self.totals = storage.Summary()
        self.previous = None
        self.reading = LiveSample()
        self.connections = None

    def change_interval(self, step):
        self.interval = max(MIN_LIVE_INTERVAL, round(self.interval + step, 10))
        self.next_sample = self.last_poll + self.interval
        return self.next_sample

    def sample(self):
        now = time.monotonic()
        previous = self.previous
        if now < self.next_sample:
            return self.reading
        self.last_poll = now
        self.next_sample = now + self.interval

        try:
            received, sent = monitor.counters(self.interface)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            self.previous = None
            self.reading = LiveSample(status='网卡读数不可用')
            return self.reading

        self.previous = now, received, sent
        if previous is None or received < previous[1] or sent < previous[2]:
            self.reading = LiveSample()
        else:
            elapsed = now - previous[0]
            up, down = sent - previous[2], received - previous[1]
            self.samples.append((time.time(), elapsed, up, down))
            upload = up * 8 / elapsed / 1e6
            download = down * 8 / elapsed / 1e6
            self.reading = LiveSample((upload, download, upload + download))
        return self.reading

    def retain_range(self, start, end):
        while self.samples and self.samples[0][0] < start:
            self.samples.popleft()
        while self.samples and self.samples[-1][0] >= end:
            self.samples.pop()
        if not self.keep_chart:
            while self.samples:
                self.totals.add_sample(*self.samples.popleft())


def time_axis(start, end, columns, zone):
    first = time_ranges.local_datetime(start, zone)
    last = time_ranges.local_datetime(end, zone)
    fmt = '%m-%d %H:%M' if first.date() != last.date() else '%H:%M:%S'
    if first.utcoffset() != last.utcoffset():
        fmt = '%H:%M %z'
    if len(first.strftime(fmt)) > columns:
        fmt = '%m-%d'
    label_size = len(first.strftime(fmt))
    count = max(1, (columns + 2) // (label_size + 2))
    axis = ['─'] * columns
    labels = [' '] * columns
    for i in range(count):
        fraction = i / (count - 1) if count > 1 else 0
        offset = round(fraction * (columns - label_size))
        position = 0 if i == 0 else columns - 1 if i == count - 1 else offset + label_size // 2
        fraction = position / (columns - 1)
        label = time_ranges.local_datetime(start + fraction * (end - start), zone).strftime(fmt)
        axis[position] = '┬'
        labels[offset:offset + len(label)] = label
    return '         └' + ''.join(axis), '          ' + ''.join(labels)


def draw_charts(start, end, step, series, columns, height, zone):
    peaks = [max(((x, value) for x, value in enumerate(values) if value is not None),
                 key=lambda item: item[1]) for values in series]
    print('网速趋势（Mbps；横轴为时间，各图独立缩放纵轴）')
    print('按采样结束时间聚合，各列按实际采样时长加权；● 标记曲线峰值。')
    print('每列约 {:.2f} 秒；空白表示无采样。'.format(step))
    # Braille cells provide 2 × 4 pixels per terminal character.
    bits = ((1, 2, 4, 64), (8, 16, 32, 128))
    pixel_height = height * 4
    axis_line, axis_labels = time_axis(start, end, columns, zone)
    for label, values, (peak_x, peak_value) in zip(('上传', '下载', '合计'), series, peaks):
        ceiling = chart_scale(peak_value)
        def pixel_y(value):
            return min(pixel_height - 1, max(0, round((1 - value / ceiling) * (pixel_height - 1))))
        grid = [[0] * columns for _ in range(height)]
        def dot(x, y):
            grid[y // 4][x // 2] |= bits[x % 2][y % 4]
        previous = None
        for x, value in enumerate(values):
            if value is None:
                previous = None
                continue
            px, py = x * 2, pixel_y(value)
            dot(px, py)
            dot(px + 1, py)
            if previous is not None:
                old_x, old_y = previous
                distance = max(px - old_x, abs(py - old_y))
                for n in range(distance + 1):
                    dot(round(old_x + (px - old_x) * n / distance),
                        round(old_y + (py - old_y) * n / distance))
            previous = px + 1, py
        peak_y = pixel_y(peak_value) // 4
        interval_start = time_ranges.local_datetime(start + peak_x * step, zone)
        interval_end = time_ranges.local_datetime(start + (peak_x + 1) * step, zone)
        time_label = '{}–{}'.format(interval_start.strftime('%H:%M:%S'), interval_end.strftime('%H:%M:%S %z'))
        print('\n{}  平滑峰值 {:,.2f} Mbps · {}'.format(label, peak_value, time_label))
        for y, row in enumerate(grid):
            cells = [chr(0x2800 + cell) if cell else ' ' for cell in row]
            if y == peak_y:
                cells[peak_x] = '●'
            print('{:8.3g} │{}'.format(ceiling * (height - 1 - y) / (height - 1), ''.join(cells)))
        print(axis_line)
        print(axis_labels)
    first_time = time_ranges.local_datetime(start, zone)
    last_time = time_ranges.local_datetime(end, zone)
    fmt = '%H:%M:%S' if first_time.date() == last_time.date() else '%m-%d %H:%M:%S'
    left_label = first_time.strftime(fmt)
    right_label = last_time.strftime(fmt)
    print('时间范围：{} → {}  时区：{}\n'.format(left_label, right_label, time_ranges.timezone_label(zone)))


def configure_parser(parser, *, live_only=False):
    query = parser.add_argument_group('查询设置')
    query.add_argument('--database', metavar='DB', default=storage.DEFAULT_DATABASE, help='SQLite 数据库路径；默认沿用 [monitor] database')
    query.add_argument('-i', '--interface', metavar='NAME', help='统计的网卡；默认使用默认路由网卡')
    query.add_argument('--timezone', '--tz', metavar='ZONE', help='查询和显示时区；默认系统时区，例如 Asia/Shanghai、UTC、+08:00')
    query.add_argument('--start', metavar='TIME', help='起始时间，包含；默认所选时区今天 00:00')
    query.add_argument('--end', metavar='TIME', help='结束时间，不包含，默认随当前时间推进' if live_only
                       else '结束时间，不包含；默认所选时区明天 00:00')
    query.add_argument('-d', '--day', metavar='YYYY-MM-DD', help='查询所选时区的指定日期，不能与 --start、--end 或 --last 同用')
    query.add_argument('--last', metavar='SPAN', help='查询最近一段时间，例如 30s、15m、2h、1d；实时模式在启动时确定起点；不能与起止时间同用')
    if not live_only:
        query.add_argument('--traffic', nargs='?', const='day', choices=TRAFFIC_PERIODS,
                           help='按周期汇总流量，默认按天，包含当前周期；也可用 --end 指定截止时间')
        query.add_argument('--count', type=int, metavar='N',
                           help='最多显示 N 个流量周期，默认 15 天、24 小时、60 分钟、60 秒或 12 个月')
    group = parser.add_argument_group('显示设置')
    chart = group.add_mutually_exclusive_group()
    chart.add_argument('--chart', dest='no_chart', action='store_false', default=False, help='显示趋势图（默认）')
    chart.add_argument('--no-chart', action='store_true', default=False, help='只显示统计表')
    group.add_argument('--width', metavar='COLS', type=int, default=None, help='趋势图列数，默认适应终端宽度，最少 10 列')
    group.add_argument('--height', metavar='ROWS', type=int, help='每个方向的图高，至少 3 行，默认铺满可用高度' if live_only
                       else '每个方向的图高，至少 3 行；历史模式最多 30 行，实时默认铺满可用高度')
    if live_only:
        parser.set_defaults(live=True, dashboard=True, plain=False)
    else:
        view = group.add_mutually_exclusive_group()
        view.add_argument('--dashboard', action='store_true', help='显示仪表盘，终端默认使用此模式')
        view.add_argument('--plain', action='store_true', help='显示原有文本格式，重定向输出默认使用此模式')
        group.add_argument('--live', action='store_true', help='实时查看网速，c 查看 TCP 连接，i 查看 IP 流量，+ 延长刷新间隔，- 缩短刷新间隔，q 退出')
    group.add_argument('--interval', metavar='SECONDS', type=float,
                       help='实时采样和刷新间隔，单位秒，默认 1 秒，最小 0.1 秒')


def run(args, parser):
    unit = getattr(args, 'traffic', None)
    count = getattr(args, 'count', None)
    if count is not None and (unit is None or count < 1):
        parser.error('--count 需要与 --traffic 同用，且必须为正整数')
    if unit and args.live:
        parser.error('--traffic 仅用于历史统计')
    if args.day is not None and any(value is not None for value in (args.start, args.end, args.last)):
        parser.error('--day 不能与 --start、--end 或 --last 同用')
    if args.interval is not None:
        if not args.live:
            parser.error('--interval 需要与 --live 同用')
        if not math.isfinite(args.interval) or args.interval < MIN_LIVE_INTERVAL:
            parser.error('--interval 必须为至少 0.1 秒的有限数值')
    if args.width is not None and args.width < 10:
        parser.error('width 不得小于 10')
    if args.height is not None and (args.height < 3 or (not args.live and args.height > 30)):
        parser.error('实时图高至少 3 行，历史图高范围为 3 到 30 行')
    terminal = sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
    dashboard = args.dashboard or (terminal and not args.plain)
    if args.live and (not terminal or not sys.stdin.isatty()):
        parser.error('实时仪表盘需要交互式终端，可用 network-monitor stats 输出统计结果')
    if args.live and args.plain:
        parser.error('--live 不能与 --plain 同用')
    if args.last is not None and (args.start is not None or args.end is not None):
        parser.error('--last 不能与 --start 或 --end 同用')
    window = None
    if args.last is not None:
        match = re.fullmatch(r'([1-9][0-9]*)([smhd])', args.last)
        if match is None:
            parser.error('--last 需要正整数和单位 s、m、h 或 d，例如 15m')
        try:
            window = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
        except ValueError:
            parser.error('--last 超出可查询的时间范围')

    try:
        zone = time_ranges.timezone(args.timezone)
        now = time.time() if unit else None
        if args.day is not None:
            initial_range = time_ranges.day_range(args.day, zone)
        elif window is not None:
            end = time.time() if now is None else now
            start = end - window
            time_ranges.local_datetime(start, zone)
            initial_range = start, end
        elif unit:
            start = time_ranges.parse_time(args.start, zone) if args.start is not None else None
            end = time_ranges.parse_time(args.end, zone) if args.end is not None else now
            if start is not None and start >= end:
                raise ValueError('开始时间必须早于结束时间；查询范围为 [start, end)')
            initial_range = start, end
        else:
            initial_range = time_ranges.resolve_range(args.start, args.end, zone)
        if unit:
            default_count, _, _ = TRAFFIC_PERIODS[unit]
            periods = time_ranges.traffic_periods(unit, count or default_count, *initial_range, zone)
    except (ValueError, OverflowError, OSError) as error:
        parser.error(str(error))
    interface = args.interface or monitor.default_interface()

    if unit:
        display = None
        current_period = None
        if dashboard:
            theme = query_terminal_theme() if terminal and 'NO_COLOR' not in os.environ else None
            display = make_dashboard(terminal, terminal_size(terminal).columns or 100, theme=theme, zone=zone)
            current_period = time_ranges.period_start(math.floor(now), unit, zone)
        print_traffic(args.database, interface, zone, unit, periods, display, current_period)
    elif args.live:
        from functools import lru_cache
        from tcp_connections import ConnectionReader

        sampler = LiveSampler(interface, args.interval or REFRESH_SECONDS, keep_chart=not args.no_chart)
        connections = ConnectionReader(interface)
        ip_capture = None
        # Socket updates can arrive between byte samples. Reuse the current SQL result.
        query = lru_cache(maxsize=1)(read_report)
        start, initial_end = initial_range
        limit = initial_end if args.end is not None or args.day is not None else math.inf
        end = start

        def frame(size, collect, view='traffic', peer_page=0, ip_sort=None, theme=None, connection_key=None):
            nonlocal end, ip_capture
            if ip_capture is None:
                preview = False
                if view == 'traffic' and not args.no_chart:
                    columns = size.columns or 100
                    columns -= 2 if columns > 2 else 0
                    rows = size.lines or 30
                    rail = metric_rail_width(columns, rows, args.width, args.height)
                    header_rows = 7 if columns >= 112 and rows >= 42 else 5
                    chart_space = rows - header_rows - 1 - (0 if rail else 9)
                    preview = ip_preview_fits(columns, rows, rail) and ip_preview_budget(chart_space, args.height)
                if view in IP_VIEWS or preview:
                    from ip_traffic import IPTraffic
                    ip_capture = IPTraffic(interface, connections.changed)
                    ip_capture.start()
            if collect:
                sampler.sample()
                if ip_capture is not None:
                    ip_capture.sample()
                query.cache_clear()
                connections.refresh()
                # Include the latest read even when it shares this frame's wall-clock time.
                now = math.nextafter(time.time(), math.inf)
                end = max(start, min(limit, now))
                if view != 'traffic':
                    sampler.retain_range(start, end)
            sampler.connections = connections.frame
            click_targets = []
            paging = list(SINGLE_PAGE)
            screen = dashboard_text(args, interface, zone, (start, end), terminal, sampler, size,
                                    query=query, view=view, peer_page=peer_page, ip_capture=ip_capture,
                                    ip_sort=ip_sort, click_targets=click_targets, theme=theme, paging=paging,
                                    connection_key=connection_key)
            return screen, sampler.next_sample, click_targets, paging
        try:
            run_live(frame, connections.changed, sampler.change_interval)
        finally:
            if ip_capture is not None:
                ip_capture.close()
    elif dashboard:
        theme = query_terminal_theme() if terminal and 'NO_COLOR' not in os.environ else None
        print(dashboard_text(args, interface, zone, initial_range, terminal, theme=theme))
    else:
        columns = args.width or max(10, shutil.get_terminal_size((80, 24)).columns - 11)
        report = read_report(args.database, interface, zone, *initial_range, None if args.no_chart else columns)
        print_plain(report, columns, args.height or 8)


def read_report(database, interface, zone, start, end, columns, sampler=None):
    history_end = end
    live_samples = ()
    if sampler is not None:
        history_end = min(end, sampler.started_at)
        sampler.retain_range(start, end)
        live_samples = sampler.samples

    with ExitStack() as resources:
        connection, tables = None, []
        if start < history_end:
            try:
                connection = resources.enter_context(storage.reader(database))
            except FileNotFoundError as error:
                if sampler is None:
                    raise RuntimeError('{}\n请先用 monitor 采集数据，或用 stats --database 指定已有的数据库。'.format(error)) from None
            else:
                tables = storage.tables_for(connection, interface, start, history_end)
        summary = storage.summarize(connection, tables, start, history_end)
        if sampler:
            summary.merge(sampler.totals)
        for sample in live_samples:
            summary.add_sample(*sample)
        chart = None
        if columns and ((summary.count and start < end) or (sampler is not None and start <= end)):
            fixed_range = sampler is not None or time_ranges.is_day_range(start, end, zone)
            chart = storage.chart_series(connection, tables, start, history_end, summary, columns,
                                         live_samples, chart_end=end if fixed_range else None)
    return Report(interface, zone, start, end, summary, chart)


def terminal_size(terminal):
    if terminal:
        try:
            return os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            pass
    return shutil.get_terminal_size((100, 30))


def background_theme(rgb):
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
    luminance = sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    return 'light' if luminance >= 0.18 else 'dark'


def reply_theme(reply):
    return background_theme(int(part, 16) / (16 ** len(part) - 1) for part in reply.groups())


def terminal_theme_hint():
    _, separator, index = os.environ.get('COLORFGBG', '').rpartition(';')
    if not separator or not re.fullmatch(r'[0-9]{1,3}', index):
        return None
    index = int(index)
    if index < 16:
        # COLORFGBG contains palette indices. These are the nominal ANSI colors.
        rgb = ('000000', '800000', '008000', '808000', '000080', '800080', '008080', 'c0c0c0',
               '808080', 'ff0000', '00ff00', 'ffff00', '0000ff', 'ff00ff', '00ffff', 'ffffff')[index]
        values = [int(rgb[i:i + 2], 16) for i in (0, 2, 4)]
    elif index < 232:
        cube = (0, 95, 135, 175, 215, 255)
        values = [cube[(index - 16) // step % 6] for step in (36, 6, 1)]
    elif index < 256:
        values = [8 + 10 * (index - 232)] * 3
    else:
        return None
    return background_theme(value / 255 for value in values)


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def enter_cbreak(descriptor, cleanup):
    import termios

    if sys.platform == 'linux' and os.uname().machine in ('x86_64', 'aarch64'):
        import fcntl
        import struct

        # Probe the kernel interface before writing so the legacy libc ABI
        # never has to round-trip the custom baud rates of a termios2 terminal.
        tcgets2, tcsets2 = 0x802c542a, 0x402c542b
        layout = struct.Struct('=IIIIB19sII')
        try:
            snapshot = fcntl.ioctl(descriptor, tcgets2, bytes(layout.size))
        except OSError as error:
            if error.errno != errno.ENOTTY:
                raise
        else:
            iflag, oflag, cflag, lflag, line, cc, ispeed, ospeed = layout.unpack(snapshot)
            lflag &= ~(termios.ICANON | termios.ECHO)
            cc = bytearray(cc)
            cc[termios.VMIN], cc[termios.VTIME] = 1, 0
            fcntl.ioctl(descriptor, tcsets2, layout.pack(iflag, oflag, cflag, lflag, line, cc, ispeed, ospeed))
            cleanup.callback(fcntl.ioctl, descriptor, tcsets2, snapshot)
            return

    import tty

    original = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor, termios.TCSANOW)
    cleanup.callback(termios.tcsetattr, descriptor, termios.TCSANOW, original)


def query_terminal_theme():
    """Ask the terminal for its default background before printing a static report."""
    import select
    import signal
    import termios

    fallback = terminal_theme_hint()
    try:
        descriptor = sys.stdin.fileno()
        if os.ttyname(descriptor) != os.ttyname(sys.stdout.fileno()):
            return fallback
        with ExitStack() as cleanup:
            previous_handler = signal.signal(signal.SIGTERM, terminate)
            cleanup.callback(signal.signal, signal.SIGTERM, previous_handler)
            enter_cbreak(descriptor, cleanup)
            if select.select([descriptor], [], [], 0)[0]:
                return fallback
            sys.stdout.write(BACKGROUND_QUERY)
            sys.stdout.flush()
            deadline = time.monotonic() + 0.12
            data = b''
            while len(data) < 128:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                    break
                chunk = os.read(descriptor, 128)
                if not chunk:
                    break
                data += chunk
                reply = BACKGROUND_REPLY.search(data)
                if reply:
                    return reply_theme(reply)
    except (OSError, ValueError, termios.error):
        pass
    return fallback


def make_dashboard(terminal, terminal_width, sampler=None, theme=None, zone=None):
    # Leave one column on each side to keep the frame off the terminal edge.
    margin = 1 if terminal and terminal_width > 2 else 0
    color = terminal and 'NO_COLOR' not in os.environ
    truecolor = os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')
    term = os.environ.get('TERM', '')
    display = Dashboard(terminal_width - 2 * margin, color=color, truecolor=truecolor, indexed='256color' in term,
                        interval=sampler.interval if sampler else REFRESH_SECONDS, theme=theme, zone=zone)
    display.margin = margin
    return display


def dashboard_text(args, interface, zone, times, terminal, sampler=None, size=None,
                   query=None, view='traffic', peer_page=0, ip_capture=None, ip_sort=None, click_targets=None,
                   theme=None, paging=None, connection_key=None):
    if size is None:
        size = terminal_size(terminal)
    rows = size.lines or 30
    display = make_dashboard(terminal, size.columns or 100, sampler, theme, zone)
    margin, width = display.margin, display.columns
    compact = width < 34 or (sampler is not None and rows < 17)
    rail = 0 if args.no_chart else metric_rail_width(width, rows, args.width, args.height)
    graph_width = width - rail - bool(rail)
    columns = None if args.no_chart or compact else min(args.width or graph_width, graph_width - 12)
    buckets = 2 * columns if sampler is not None and columns else columns
    targets = [] if click_targets is not None else None
    if view in IP_VIEWS:
        screen = display.ip_screen(ip_capture, rows, peer_page, ip_sort, targets, view=view)
    elif view == 'connections':
        screen = display.connection_screen(sampler.connections, rows, peer_page, targets, selected=connection_key)
    else:
        report = (query or read_report)(args.database, interface, zone, *times, buckets, sampler)
        screen = display.render(report, args.height, live_sample=sampler.reading if sampler else None,
                                rows=rows, rail=rail, connections=sampler.connections if sampler else None,
                                click_targets=targets, ip_capture=ip_capture, ip_page=peer_page, ip_sort=ip_sort)
    if click_targets is not None:
        click_targets.extend((row, left + margin, right + margin, action) for row, left, right, action in targets)
    if paging is not None:
        paging[:] = display.paging
    return '\n'.join(' ' * margin + line for line in screen.split('\n'))


def live_update(lines, previous, terminal_rows):
    updates = []
    for row, line in enumerate(lines, start=1):
        if row > len(previous) or line != previous[row - 1]:
            # Clear first so a full-width row keeps its last cell.
            updates.append('\033[{};1H\033[K{}'.format(row, line))
    if len(lines) < terminal_rows and (not previous or len(lines) < len(previous)):
        updates.append('\033[{};1H\033[J'.format(len(lines) + 1))
    if not updates:
        return ''
    # Synchronized output lets supporting terminals present the whole update at once.
    return '\033[?2026h' + ''.join(updates) + '\033[?2026l'


def live_input(data):
    """Decode terminal input, keeping a partial escape sequence for the next read."""
    events = []
    offset = 0
    while offset < len(data):
        if data[offset] != 27:
            events.append(chr(data[offset]).lower())
            offset += 1
            continue
        if data.startswith(b'\x1b[M', offset):
            if len(data) - offset < 6:
                break
            button, x, y = data[offset + 3:offset + 6]
            if button == 32:
                events.append((x - 33, y - 33))
            offset += 6
            continue
        sequence = ESCAPE_INPUT.match(data, offset)
        if sequence is None:
            if len(data) - offset <= 64:
                break
            offset += 1
            continue
        click = MOUSE_PRESS.fullmatch(sequence[0])
        if click:
            x, y = (int(value) - 1 for value in click.groups())
            events.append((x, y))
        reply = BACKGROUND_REPLY.fullmatch(sequence[0])
        if reply:
            events.append(reply_theme(reply))
        offset = sequence.end()
    return events, data[offset:]


def run_live(render, changed, change_interval):
    import select
    import signal
    import termios

    descriptor = sys.stdin.fileno()
    cleanup = ExitStack()
    screen_open = False
    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            enter_cbreak(descriptor, cleanup)
        except (OSError, termios.error) as error:
            raise RuntimeError('无法设置实时仪表盘的终端按键模式：{}'.format(error)) from error
        screen_open = True
        sys.stdout.write('\033[?1049h\033[?25l' + ENABLE_MOUSE)
        color = 'NO_COLOR' not in os.environ
        theme = terminal_theme_hint() if color else None
        if color:
            sys.stdout.write(BACKGROUND_QUERY)
        lines = []
        size = terminal_size(True)
        deadline = -math.inf
        resize_started = None
        resize_deadline = math.inf
        redraw = False
        view = 'traffic'
        ip_view = 'ip'
        connection_key = None
        overview_page = 0
        peer_page, page_count = SINGLE_PAGE
        ip_sorts = {}
        click_targets = []
        pending_input = b''
        while True:
            now = time.monotonic()
            collect = now >= deadline
            if collect or redraw or now >= resize_deadline or (changed.is_set() and resize_started is None):
                changed.clear()
                previous = lines if resize_started is None else []
                sort_view = 'ip-local' if view == 'ip-local' else 'ip'
                screen, deadline, click_targets, paging = render(size, collect, view, peer_page,
                                                                 ip_sorts.get(sort_view), theme, connection_key)
                peer_page, page_count = paging
                lines = screen.split('\n')
                update = live_update(lines, previous, size.lines or 30)
                if update:
                    sys.stdout.write(update)
                    sys.stdout.flush()
                redraw = False
                resize_started = None
                resize_deadline = math.inf

            remaining = max(0, min(deadline, resize_deadline) - time.monotonic())
            if select.select([descriptor], [], [], min(remaining, RESIZE_POLL_SECONDS))[0]:
                try:
                    data = os.read(descriptor, 128)
                except OSError as error:
                    if error.errno == errno.EIO:
                        return
                    raise
                if not data:
                    return
                events, pending_input = live_input(pending_input + data)
                for key in events:
                    if key in ('light', 'dark'):
                        if color and theme != key:
                            theme = key
                            redraw = True
                        continue
                    if view == 'connections' and connection_key is None and isinstance(key, str) and key in '123456789':
                        addresses = list(dict.fromkeys(action for _, _, _, action in click_targets
                                                       if isinstance(action, tuple) and action[0] == 'tcp-address'))
                        index = int(key) - 1
                        if index >= len(addresses):
                            continue
                        key = addresses[index]
                    elif isinstance(key, tuple):
                        x, y = key
                        key = next((action for row, left, right, action in click_targets
                                    if row == y and left <= x < right), None)
                        if key is None:
                            continue
                        if y != len(lines) - 1 and not isinstance(key, tuple) and key not in ('p', 'n'):
                            if view == 'traffic' or view in IP_VIEWS:
                                sort_view = 'ip-local' if view == 'ip-local' else 'ip'
                                ip_sort = ip_sorts.get(sort_view, ('address', False) if sort_view == 'ip-local' else None)
                                descending = not ip_sort[1] if ip_sort and ip_sort[0] == key else key != 'address'
                                ip_sorts[sort_view] = key, descending
                                peer_page = 0
                                redraw = True
                            continue
                    if isinstance(key, tuple) and key[0] == 'tcp-address':
                        if view != 'connections' or connection_key is not None:
                            continue
                        overview_page = peer_page
                        connection_key = key[1:]
                        peer_page, page_count = SINGLE_PAGE
                        redraw = True
                        continue
                    if key == 'q':
                        return
                    if key in ('c', 'i'):
                        target = ip_view if key == 'i' else 'connections'
                        view = 'traffic' if view == target else target
                        connection_key = None
                        peer_page, page_count = SINGLE_PAGE
                    elif key == 'b' and view == 'connections' and connection_key is not None:
                        connection_key = None
                        peer_page = overview_page
                    elif key == 'm' and view in IP_VIEWS:
                        ip_view = 'ip' if view == 'ip-local' else 'ip-local'
                        view = ip_view
                        peer_page, page_count = SINGLE_PAGE
                    elif key in '+-':
                        deadline = change_interval(MIN_LIVE_INTERVAL if key == '+' else -MIN_LIVE_INTERVAL)
                    elif key in 'pn':
                        next_page = peer_page + (1 if key == 'n' else -1)
                        if not 0 <= next_page < page_count:
                            continue
                        peer_page = next_page
                    else:
                        continue
                    redraw = True

            measured = terminal_size(True)
            if measured != size:
                size = measured
                now = time.monotonic()
                if resize_started is None:
                    resize_started = now
                # Redraw after the changes settle, or periodically during a long drag.
                resize_deadline = min(now + RESIZE_DEBOUNCE_SECONDS, resize_started + RESIZE_MAX_DELAY_SECONDS)
    finally:
        try:
            with suppress(OSError, termios.error):
                cleanup.close()
            if screen_open:
                with suppress(OSError):
                    sys.stdout.write(DISABLE_MOUSE + '\033[?2026l\033[?25h\033[?1049l')
                    sys.stdout.flush()
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


def print_plain(report, columns, height):
    interface, zone = report.interface, report.zone
    start, end, summary, chart = report.start, report.end, report.summary, report.chart
    print('网卡：{}    时区：{}'.format(interface, time_ranges.timezone_label(zone)))
    print('查询范围：[{}, {})'.format(
        time_ranges.local_datetime(start, zone).strftime('%Y-%m-%d %H:%M:%S %z'),
        time_ranges.local_datetime(end, zone).strftime('%Y-%m-%d %H:%M:%S %z')))
    count, duration = summary.count, summary.duration
    totals, minima, maxima = summary.totals, summary.minima, summary.maxima
    if not count:
        print('暂无采样数据')
        return
    whole_seconds = int(duration)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed = '{} 小时 {:02d} 分 {:02d} 秒'.format(hours, minutes, seconds)
    print('\n网络速度统计')
    print('网卡：{}    采样：{:,} 次'.format(interface, count))
    print('时间：{} → {}'.format(time_ranges.local_datetime(summary.first, zone).strftime('%Y-%m-%d %H:%M:%S %z'),
                                 time_ranges.local_datetime(summary.last, zone).strftime('%Y-%m-%d %H:%M:%S %z')))
    print('有效时长：{}    平均采样间隔：{:.2f} 秒'.format(elapsed, duration / count))
    rows = [['方向', '平均 Mbps', '峰值 Mbps', '最低 Mbps', '累计流量']]
    for i, label in enumerate(('上传', '下载', '合计')):
        rows.append([label, '{:,.2f}'.format(totals[i] * 8 / duration / 1e6),
                     '{:,.2f}'.format(maxima[i]), '{:,.2f}'.format(minima[i]),
                     traffic(totals[i])])
    sizes = [max(display_width(row[i]) for row in rows) for i in range(5)]

    def border(left, middle, right):
        return left + middle.join('─' * (size + 2) for size in sizes) + right

    print(border('┌', '┬', '┐'))
    for n, row in enumerate(rows):
        if n == 1 or n == 3:
            print(border('├', '┼', '┤'))
        print('│ ' + ' │ '.join(pad(cell, sizes[i], right=i > 0)
                                 for i, cell in enumerate(row)) + ' │')
    print(border('└', '┴', '┘'))
    print('平均值按采样时长加权；峰值为单个采样区间的平均速率。')
    print('Mbps = 百万比特/秒；流量按 1000 进制换算。\n')
    if chart is not None:
        draw_charts(*chart, columns, height, zone)


def traffic_rows(database, interface, zone, unit, periods):
    """Return (label, totals, period boundary) from the first period with samples onwards."""
    _, _, date_format = TRAFFIC_PERIODS[unit]
    rows = []
    with storage.reader(database) as connection:
        for boundary, start, end in periods:
            totals = storage.traffic_totals(connection, interface, start, end)
            if totals is None and not rows:
                continue
            rows.append((time_ranges.local_datetime(boundary, zone).strftime(date_format), totals, boundary))
    return rows


def print_traffic(database, interface, zone, unit, periods, display=None, current_period=None):
    _, label, _ = TRAFFIC_PERIODS[unit]
    rows = traffic_rows(database, interface, zone, unit, periods)
    if display is not None:
        table = display.traffic_table(interface, time_ranges.timezone_label(zone), label, rows, current_period)
        print('\n'.join(' ' * display.margin + line for line in table.split('\n')))
        return
    print('流量汇总（按{}）  网卡 {}  时区 {}'.format(label, interface, time_ranges.timezone_label(zone)))
    if not rows:
        print('暂无采样数据')
        return
    table = [('周期起点', '下载', '上传', '合计')]
    table.extend((stamp, *([traffic(totals[i]) for i in (1, 0, 2)] if totals is not None else ['—'] * 3))
                 for stamp, totals, _ in rows)
    widths = [max(map(display_width, column)) for column in zip(*table)]
    for row in table:
        print('  '.join(pad(value, width, right=i > 0) for i, (value, width) in enumerate(zip(row, widths))))


def main(argv=None):
    from network_monitor import main as cli_main
    return cli_main(['stats'] + (sys.argv[1:] if argv is None else list(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
