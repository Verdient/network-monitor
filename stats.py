#!/usr/bin/env python3
"""Query daily SQLite partitions and display statistics in the selected zone."""
from collections import deque
from contextlib import ExitStack
import math
import os
import re
import shutil
import subprocess
import sys
import time

import monitor
from stats_display import Dashboard, LiveSample, Report, chart_scale, display_width, metric_rail_width, pad, traffic
import storage
import time_ranges


REFRESH_SECONDS = 1.0
MIN_LIVE_INTERVAL = 0.1
RESIZE_POLL_SECONDS = 0.04
RESIZE_DEBOUNCE_SECONDS = 0.075
RESIZE_MAX_DELAY_SECONDS = 0.2


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
    print('时间范围：{} → {}（{}）\n'.format(left_label, right_label, zone or '系统时区'))


def configure_parser(parser, *, live_only=False):
    query = parser.add_argument_group('查询设置')
    query.add_argument('-d', '--database', metavar='DB', default=storage.DEFAULT_DATABASE, help='SQLite 数据库路径；默认沿用 [monitor] database')
    query.add_argument('-i', '--interface', metavar='NAME', help='统计的网卡；默认使用默认路由网卡')
    query.add_argument('--timezone', '--tz', metavar='ZONE', help='查询和显示时区；默认系统时区，例如 Asia/Shanghai、UTC、+08:00')
    query.add_argument('--start', metavar='TIME', help='起始时间，包含；默认所选时区今天 00:00')
    query.add_argument('--end', metavar='TIME', help='结束时间，不包含，默认随当前时间推进' if live_only
                       else '结束时间，不包含；默认所选时区明天 00:00')
    query.add_argument('--last', metavar='SPAN', help='查询最近一段时间，例如 30s、15m、2h、1d；实时模式在启动时确定起点；不能与起止时间同用')
    group = parser.add_argument_group('显示设置')
    chart = group.add_mutually_exclusive_group()
    chart.add_argument('--no-chart', action='store_true', default=False, help='只显示统计表')
    chart.add_argument('--chart', dest='no_chart', action='store_false', default=False, help='显示趋势图（默认）')
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

    def query_range(zone):
        if window is None:
            return time_ranges.resolve_range(args.start, args.end, zone)
        end = time.time()
        start = end - window
        time_ranges.local_datetime(start, zone)
        return start, end

    try:
        zone = time_ranges.timezone(args.timezone)
        initial_range = query_range(zone)
    except (ValueError, OverflowError, OSError) as error:
        parser.error(str(error))
    interface = args.interface or monitor.default_interface()

    if args.live:
        from functools import lru_cache
        from tcp_connections import ConnectionReader

        sampler = LiveSampler(interface, args.interval or REFRESH_SECONDS, keep_chart=not args.no_chart)
        connections = ConnectionReader()
        ip_capture = None
        # Socket updates can arrive between byte samples. Reuse the current SQL result.
        query = lru_cache(maxsize=1)(read_report)
        start, initial_end = initial_range
        limit = initial_end if args.end is not None else math.inf
        end = start

        def frame(size, collect, view='traffic', peer_page=0):
            nonlocal end, ip_capture
            if view == 'ip' and ip_capture is None:
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
            screen = dashboard_text(args, interface, zone, (start, end), terminal, sampler, size,
                                    query=query, view=view, peer_page=peer_page, ip_capture=ip_capture)
            return screen, sampler.next_sample
        try:
            run_live(frame, connections.changed, sampler.change_interval)
        finally:
            if ip_capture is not None:
                ip_capture.close()
    elif dashboard:
        print(dashboard_text(args, interface, zone, initial_range, terminal))
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
                    raise RuntimeError('{}\n请先用 monitor 采集数据，或用 stats -d 指定已有的数据库。'.format(error)) from None
            else:
                tables = storage.tables_for(connection, interface, start, history_end)
        summary = storage.summarize(connection, tables, start, history_end)
        if sampler:
            summary.merge(sampler.totals)
        for sample in live_samples:
            summary.add_sample(*sample)
        chart = None
        if columns and ((summary.count and start < end) or (sampler is not None and start <= end)):
            chart = storage.chart_series(connection, tables, start, history_end, summary, columns,
                                         live_samples, chart_end=end if sampler else None)
    return Report(interface, zone, start, end, summary, chart)


def terminal_size(terminal):
    if terminal:
        try:
            return os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            pass
    return shutil.get_terminal_size((100, 30))


def dashboard_text(args, interface, zone, times, terminal, sampler=None, size=None,
                   query=None, view='traffic', peer_page=0, ip_capture=None):
    if size is None:
        size = terminal_size(terminal)
    terminal_width = size.columns or 100
    rows = size.lines or 30
    # Leave one column on each side to keep the frame off the terminal edge.
    margin = 1 if terminal and terminal_width > 2 else 0
    width = terminal_width - 2 * margin
    compact = width < 34 or (sampler is not None and rows < 17)
    rail = 0 if args.no_chart else metric_rail_width(width, rows, args.width, args.height)
    graph_width = width - rail - bool(rail)
    columns = None if args.no_chart or compact else min(args.width or graph_width, graph_width - 12)
    buckets = 2 * columns if sampler is not None and columns else columns
    color = terminal and 'NO_COLOR' not in os.environ
    truecolor = os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')
    display = Dashboard(width, color=color, truecolor=truecolor, indexed='256color' in os.environ.get('TERM', ''),
                        interval=sampler.interval if sampler else REFRESH_SECONDS)
    if view == 'ip':
        screen = display.ip_screen(ip_capture, rows, peer_page)
    elif view == 'connections':
        screen = display.connection_screen(sampler.connections, rows, peer_page)
    else:
        report = (query or read_report)(args.database, interface, zone, *times, buckets, sampler)
        screen = display.render(report, args.height, live_sample=sampler.reading if sampler else None,
                                rows=rows, rail=rail, connections=sampler.connections if sampler else None)
    return '\n'.join(' ' * margin + line for line in screen.split('\n'))


def live_update(lines, previous, terminal_rows):
    updates = []
    for row, line in enumerate(lines, start=1):
        if row > len(previous) or line != previous[row - 1]:
            updates.append('\033[{};1H{}\033[K'.format(row, line))
    if len(lines) < terminal_rows and (not previous or len(lines) < len(previous)):
        updates.append('\033[{};1H\033[J'.format(len(lines) + 1))
    if not updates:
        return ''
    # Synchronized output lets supporting terminals present the whole update at once.
    return '\033[?2026h' + ''.join(updates) + '\033[?2026l'


def run_live(render, changed, change_interval):
    import select
    import signal
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    attributes = termios.tcgetattr(descriptor)

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        tty.setcbreak(descriptor)
        sys.stdout.write('\033[?1049h\033[?25l')
        lines = []
        size = terminal_size(True)
        deadline = -math.inf
        resize_started = None
        resize_deadline = math.inf
        redraw = False
        view = 'traffic'
        peer_page = 0
        while True:
            now = time.monotonic()
            collect = now >= deadline
            if collect or redraw or now >= resize_deadline or (changed.is_set() and resize_started is None):
                changed.clear()
                previous = lines if resize_started is None else []
                screen, deadline = render(size, collect, view, peer_page)
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
                keys = os.read(descriptor, 32).lower()
                if not keys or b'q' in keys:
                    return
                for key in keys.decode('ascii', errors='ignore'):
                    if key in 'ci':
                        target = 'ip' if key == 'i' else 'connections'
                        view = 'traffic' if view == target else target
                        peer_page = 0
                    elif key in '+-':
                        deadline = change_interval(MIN_LIVE_INTERVAL if key == '+' else -MIN_LIVE_INTERVAL)
                    elif key in 'jk' and view != 'traffic':
                        peer_page += 1 if key == 'j' else -1
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
        termios.tcsetattr(descriptor, termios.TCSADRAIN, attributes)
        sys.stdout.write('\033[?2026l\033[?25h\033[?1049l')
        sys.stdout.flush()
        signal.signal(signal.SIGTERM, previous_handler)


def print_plain(report, columns, height):
    interface, zone = report.interface, report.zone
    start, end, summary, chart = report.start, report.end, report.summary, report.chart
    print('网卡：{}    时区：{}'.format(interface, zone or '系统时区'))
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


def main(argv=None):
    from network_monitor import main as cli_main
    return cli_main(['stats'] + (sys.argv[1:] if argv is None else list(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
