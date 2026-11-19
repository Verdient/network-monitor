#!/usr/bin/env python3
"""Query daily SQLite partitions and display statistics in the selected zone."""
import math
import unicodedata
import shutil
import sys

import monitor
import storage
import time_ranges


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
    ceiling = max(value for _, value in peaks) or 1
    magnitude = 10 ** math.floor(math.log10(ceiling))
    ceiling = math.ceil(ceiling / magnitude) * magnitude
    print('网速趋势（Mbps；横轴为时间，三图使用相同纵轴）')
    print('按采样结束时间聚合，各列按实际采样时长加权；● 标记曲线峰值。')
    print('每列约 {:.2f} 秒；空白表示无采样。'.format(step))
    # Braille cells provide 2 × 4 pixels per terminal character.
    bits = ((1, 2, 4, 64), (8, 16, 32, 128))
    pixel_height = height * 4
    def pixel_y(value):
        return min(pixel_height - 1, max(0, round((1 - value / ceiling) * (pixel_height - 1))))
    axis_line, axis_labels = time_axis(start, end, columns, zone)
    for label, values, (peak_x, peak_value) in zip(('上传', '下载', '合计'), series, peaks):
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
                cells[peak_x] = '\033[1;91m●\033[0m' if sys.stdout.isatty() else '●'
            print('{:8.1f} │{}'.format(ceiling * (height - 1 - y) / (height - 1), ''.join(cells)))
        print(axis_line)
        print(axis_labels)
    first_time = time_ranges.local_datetime(start, zone)
    last_time = time_ranges.local_datetime(end, zone)
    fmt = '%H:%M:%S' if first_time.date() == last_time.date() else '%m-%d %H:%M:%S'
    left_label = first_time.strftime(fmt)
    right_label = last_time.strftime(fmt)
    print('时间范围：{} → {}（{}）\n'.format(left_label, right_label, zone or '系统时区'))


def configure_parser(parser):
    query = parser.add_argument_group('查询设置')
    query.add_argument('-d', '--database', metavar='DB', default=storage.DEFAULT_DATABASE, help='SQLite 数据库路径；默认沿用 [monitor] database')
    query.add_argument('-i', '--interface', metavar='NAME', help='统计的网卡；默认使用默认路由网卡')
    query.add_argument('--timezone', '--tz', metavar='ZONE', help='查询和显示时区；默认系统时区，例如 Asia/Shanghai、UTC、+08:00')
    query.add_argument('--start', metavar='TIME', help='起始时间，包含；默认所选时区今天 00:00')
    query.add_argument('--end', metavar='TIME', help='结束时间，不包含；默认所选时区明天 00:00')
    group = parser.add_argument_group('显示设置')
    chart = group.add_mutually_exclusive_group()
    chart.add_argument('--no-chart', action='store_true', default=False, help='只显示统计表')
    chart.add_argument('--chart', dest='no_chart', action='store_false', default=False, help='显示趋势图（默认）')
    group.add_argument('--width', metavar='COLS', type=int, default=None, help='趋势图列数，默认适应终端宽度，最少 10 列')
    group.add_argument('--height', metavar='ROWS', type=int, default=8, help='每幅趋势图高度，范围 3–30 行')


def run(args, parser):
    if args.width is not None and args.width < 10:
        parser.error('width 不得小于 10')
    if not 3 <= args.height <= 30:
        parser.error('height 范围为 3 到 30')
    try:
        zone = time_ranges.timezone(args.timezone)
        start, end = time_ranges.resolve_range(args.start, args.end, zone)
    except (ValueError, OverflowError, OSError) as error:
        parser.error(str(error))
    interface = args.interface or monitor.default_interface()
    columns = args.width or max(10, shutil.get_terminal_size((80, 24)).columns - 11)
    with storage.reader(args.database) as connection:
        tables = storage.tables_for(connection, interface, start, end)
        summary = storage.summarize(connection, tables, start, end)
        chart = storage.chart_series(connection, tables, start, end, summary, columns) if summary.count and not args.no_chart else None
    print('网卡：{}    时区：{}'.format(interface, zone or '系统时区'))
    print('查询范围：[{}, {})'.format(
        time_ranges.local_datetime(start, zone).strftime('%Y-%m-%d %H:%M:%S %z'),
        time_ranges.local_datetime(end, zone).strftime('%Y-%m-%d %H:%M:%S %z')))
    count, duration = summary.count, summary.duration
    totals, minima, maxima = summary.totals, summary.minima, summary.maxima
    if not count:
        print('暂无采样数据')
        return
    def width(text):
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
                   for c in text)

    def pad(text, size, right=False):
        spaces = ' ' * max(0, size - width(text))
        return spaces + text if right else text + spaces

    def traffic(value):
        for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
            if value < 1000 or unit == 'PB':
                return '{:,.2f} {}'.format(value, unit)
            value /= 1000

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
    sizes = [max(width(row[i]) for row in rows) for i in range(5)]

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
        draw_charts(*chart, columns, args.height, zone)


def main(argv=None):
    from network_monitor import main as cli_main
    return cli_main(['stats'] + (sys.argv[1:] if argv is None else list(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
