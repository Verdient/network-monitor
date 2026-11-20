"""Render a terminal dashboard without terminal or database dependencies."""

from dataclasses import dataclass
from itertools import groupby, zip_longest
import math
import re
import unicodedata

from time_ranges import local_datetime


ANSI = re.compile(r'\x1b\[[0-9;]*m')
COLORS = {
    'frame': '38;2;105;102;153', 'grid': '38;2;81;85;112', 'muted': '38;2;169;175;201',
    'ink': '1;38;2;238;244;255', 'down': '1;38;2;79;248;238',
    'up': '1;38;2;255;105;203', 'total': '1;38;2;255;207;107',
}
BACKGROUNDS = {'ink': '48;2;16;18;37', 'down': '48;2;10;35;47',
               'up': '48;2;41;20;45', 'total': '48;2;40;31;37'}
BASIC = {
    'frame': '90', 'grid': '90', 'muted': '37', 'ink': '1;37', 'down': '1;36',
    'up': '1;35', 'total': '1;33',
}
# Each stop pairs a true-color shade with its xterm-256 equivalent.
WAVE_SHADES = {
    'down': (((23, 112, 141), 24), ((23, 139, 158), 30), ((29, 166, 177), 37),
             ((39, 194, 196), 44), ((74, 218, 214), 80), ((126, 238, 230), 87),
             ((182, 250, 242), 159), ((224, 255, 250), 195)),
    'up': (((147, 57, 118), 89), ((170, 67, 134), 126), ((195, 79, 151), 162),
           ((219, 99, 170), 169), ((238, 128, 190), 206), ((251, 161, 210), 212),
           ((255, 195, 227), 218), ((255, 226, 244), 225)),
}
DIRECTIONS = (('↓ 下载', 1, 'down'), ('↑ 上传', 0, 'up'), ('∑ 合计', 2, 'total'))
CHANNELS = ('RX', 'TX', 'Σ')
# Three terminal rows encode a five-pixel-high digit using half blocks.
DIGITS = {
    '0': ('█▀█', '█ █', '▀▀▀'), '1': ('▄█ ', ' █ ', '▀▀▀'),
    '2': ('▀▀█', '█▀▀', '▀▀▀'), '3': ('▀▀█', '▀▀█', '▀▀▀'),
    '4': ('█ █', '▀▀█', '  ▀'), '5': ('█▀▀', '▀▀█', '▀▀▀'),
    '6': ('█▀▀', '█▀█', '▀▀▀'), '7': ('▀▀█', ' █ ', ' ▀ '),
    '8': ('█▀█', '█▀█', '▀▀▀'), '9': ('█▀█', '▀▀█', '▀▀▀'),
    '.': (' ', ' ', '▀'),
}


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in ANSI.sub('', text) if not unicodedata.combining(char))


def pad(text, width, right=False):
    spaces = ' ' * max(0, width - display_width(text))
    return spaces + text if right else text + spaces


def clip(text, width):
    if width <= 0:
        return ''
    if display_width(text) <= width:
        return text
    used, result = 0, []
    for char in text:
        size = display_width(char)
        if used + size >= width:
            break
        result.append(char)
        used += size
    return ''.join(result) + '…'


def clean(text):
    return ''.join(char if char.isprintable() else '?' for char in str(text))


def traffic(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if value < 1000 or unit == 'PB':
            return '{:,.2f} {}'.format(value, unit)
        value /= 1000


def speed(mbps):
    if 0 < mbps < 0.001:
        value, unit = mbps * 1e6, 'bps'
    elif 0 < mbps < 1:
        value, unit = mbps * 1000, 'Kbps'
    elif mbps < 1000:
        value, unit = mbps, 'Mbps'
    else:
        value, unit = mbps / 1000, 'Gbps'
    return '{:,.2f} {}'.format(value, unit)


def elapsed(duration):
    hours, remainder = divmod(int(duration), 3600)
    minutes, seconds = divmod(remainder, 60)
    return '{}h {:02d}m {:02d}s'.format(hours, minutes, seconds)


@dataclass
class Report:
    interface: str
    zone: object
    start: float
    end: float
    summary: object
    chart: tuple = None


@dataclass(frozen=True)
class LiveSample:
    rates: tuple = None
    status: str = '等待采样'


def metric_rail_width(columns, rows, requested_width, requested_height):
    # Three full-size cards need 27 rows, plus the header and live controls.
    rail = 36
    if columns >= 112 and rows >= 34 and requested_height is None:
        if requested_width is None or requested_width <= columns - rail - 1 - 12:
            return rail
    return 0


class Dashboard:
    def __init__(self, columns, color=False, truecolor=False, indexed=False, interval=1.0):
        self.columns = columns
        self.interval = interval
        self.colors = (COLORS if truecolor else BASIC) if color else {}
        self.truecolor = color and truecolor
        self.title_ink = ''
        if self.truecolor:
            self.title_ink = '1;38;2;247;250;255;48;2;9;13;24'
        elif color and indexed:
            self.title_ink = '1;38;5;231;48;5;233'
        elif color:
            self.title_ink = '1;97;40'
        self.wave_inks = {}
        for role, stops in WAVE_SHADES.items():
            if self.truecolor:
                inks = ['38;2;{};{};{}'.format(*rgb) for rgb, _ in stops]
            elif color and indexed:
                inks = ['38;5;{}'.format(index) for _, index in stops]
            elif color:
                normal, bright = ('36', '96') if role == 'down' else ('35', '95')
                inks = [normal] * 4 + [bright] * 4
            else:
                inks = [''] * len(stops)
            self.wave_inks[role] = inks

    @staticmethod
    def tint(text, code):
        return '\033[{}m{}\033[0m'.format(code, text) if code and text else text

    def paint(self, text, role, reverse=False):
        code = self.colors.get(role)
        if code and reverse:
            code += ';7'
        return self.tint(text, code)

    def surface(self, text, role):
        if not self.colors:
            return text
        background = BACKGROUNDS[role] if self.truecolor else '40'
        base = '\033[{};{}m'.format(background, self.colors['muted'])
        # Inline accents reset SGR. Restore the panel background after each one.
        return base + text.replace('\033[0m', '\033[0m' + base) + '\033[0m'

    def live_readings(self, sample, width):
        if sample.rates is None:
            return [self.paint(clip('当前  ' + sample.status, width), 'ink')]
        readings = [(label + '  ' + speed(sample.rates[i]), role) for label, i, role in DIRECTIONS]
        prefix, separator = '当前  ', '  ·  '
        combined = prefix + separator.join(text for text, _ in readings)
        if display_width(combined) <= width:
            return [self.paint(prefix, 'ink') + separator.join(self.paint(text, role) for text, role in readings)]
        indent = ' ' * display_width(prefix)
        return [self.paint(clip((prefix if i == 0 else indent) + text, width), role)
                for i, (text, role) in enumerate(readings)]

    def panel(self, title, lines, width=None, role='ink', detail='', detail_role='muted'):
        width = self.columns if width is None else width
        frame = lambda text: self.paint(text, 'frame')
        title = ' {} '.format(clip(title, width - 8))
        room = width - 6 - display_width(title)
        trailer = ' {} '.format(detail) if detail and display_width(detail) + 3 <= room else ''
        top = (self.paint('┏━', role) + self.tint(title, self.title_ink) + self.paint('╲', role)
               + frame('─' * (room - display_width(trailer))) + self.paint(trailer, detail_role)
               + self.paint('╱╱┓', role))
        body = [frame('│') + ' ' + pad(line, width - 4) + ' ' + frame('│') for line in lines]
        bottom = frame('└' + '─' * (width - 4)) + self.paint('╴╱┘', role)
        return [self.surface(line, role) for line in (top, *body, bottom)]

    def digits(self, reading, role, width):
        number, _, unit = reading.replace(',', '').partition(' ')
        if not unit or any(char not in DIGITS for char in number):
            return []
        rows = [' '.join(DIGITS[char][y] for char in number) for y in range(3)]
        if len(rows[0]) + len(unit) + 1 > width:
            return []
        return [self.paint(row, role) + (self.paint(' ' + unit, 'ink') if y == 2 else '')
                for y, row in enumerate(rows)]

    def metrics(self, summary, compact=False, rates=None, big_readout=False, rail=0):
        totals = [traffic(v) for v in summary.totals] if summary.count else ['—'] * 3
        average = [speed(v * 8 / summary.duration / 1e6) for v in summary.totals] if summary.count else ['—'] * 3
        peaks = [speed(v) for v in summary.maxima] if summary.count else ['—'] * 3
        lows = [speed(v) for v in summary.minima] if summary.count else ['—'] * 3
        if rates is not None:
            rates = [speed(v) if v is not None else '—' for v in rates]

        def readings(i, width):
            values = [('平均', average[i]), ('峰值', peaks[i]), ('最低', lows[i])]
            return [self.paint(label, 'muted') + pad(clip(value, width - 5), width - 4, right=True)
                    for label, value in values]

        if self.columns >= 80:
            cards = []
            widths = [rail] * 3 if rail else [(self.columns - 2 + i) // 3 for i in range(3)]
            for (label, i, role), width, code in zip(DIRECTIONS, widths, CHANNELS):
                inside = width - 4
                values = [('累计', totals[i]), ('平均', average[i])]
                if rates is not None:
                    values.insert(0, ('当前', rates[i]))
                if not compact:
                    values.extend([('峰值', peaks[i]), ('最低', lows[i])])
                key, reading = values[0]
                lines = []
                detail = code if rates is None else ''
                if self.colors and big_readout:
                    lines = self.digits(reading, role, inside) or ['', self.paint(clip(reading, inside), role), '']
                    label += ' · ' + key
                    values = values[1:]
                for key, value in values:
                    value = pad(clip(value, inside - 5), inside - 4, right=True)
                    lines.append(self.paint(key, 'muted') + self.paint(value, role if not lines else 'ink'))
                cards.append(self.panel(label, lines, width=width, role=role, detail=detail, detail_role=role))
            if rail:
                return [line for card in cards for line in card]
            return [' '.join(row) for row in zip(*cards)]

        rows = []
        available = self.columns - 4
        if rates is not None:
            headings = ['方向', '当前', '累计', '平均']
            data = [[label, rates[i], totals[i], average[i]]
                    for label, i, _ in DIRECTIONS]
            if not compact:
                headings += ['峰值', '最低']
                for row, (_, i, _) in zip(data, DIRECTIONS):
                    row += [peaks[i], lows[i]]
            sizes = [max(display_width(value) for value in col) for col in zip(headings, *data)]
            if sum(sizes) + 2 * (len(sizes) - 1) <= available:
                sizes[0] += available - sum(sizes) - 2 * (len(sizes) - 1)
                rows.append(self.paint('  '.join(pad(key, w, right=n > 0)
                                                 for n, (key, w) in enumerate(zip(headings, sizes))), 'muted'))
                for row, (_, _, role) in zip(data, DIRECTIONS):
                    rows.append('  '.join(self.paint(pad(value, w, right=n > 0), role if n < 2 else 'ink')
                                          for n, (value, w) in enumerate(zip(row, sizes))))
                return self.panel('流量与速率', rows)
        for label, i, role in DIRECTIONS:
            total = totals[i]
            if compact:
                total += ' · ' + average[i]
            if rates is not None:
                rows.append(self.paint(label, role) + pad(rates[i], available - display_width(label), True))
                label = '累计'
            rows.append(self.paint(label, role) + pad(clip(total, available - 8), available - display_width(label), True))
            if compact:
                continue
            values = '平均 {}  峰值 {}  最低 {}'.format(average[i], peaks[i], lows[i])
            if display_width(values) <= available:
                rows.append(self.paint(values, 'muted'))
            else:
                rows.extend(readings(i, available))
        title = '流量与平均速率' if compact else '流量与速率'
        return self.panel(title, rows)

    def graph(self, chart, height, zone, live=False, totals=None, width=None, extra_row=False):
        width = self.columns if width is None else width
        start, end, step, series = chart
        stride = 2 if live else 1
        columns = (len(series[0]) + stride - 1) // stride
        # Give the numeric axis an extra column when needed to center the graph.
        axis_width = 8 + (width - columns) % 2
        graph_width = axis_width + columns
        area = []
        time_ticks, labels = time_labels(start, end, columns, zone)
        side = max(0, (width - 4 - graph_width) // 2)
        peaks = [max((v for v in values if v is not None), default=0) for values in series[:2]]
        color_peak = max(peaks) or 1

        def axis(label, tick):
            return self.paint(pad(label, axis_width - 1, True), 'muted') + self.paint(tick, 'frame')

        def graph_line(cells, tick, label, role, bands, level_band, note):
            background = '┄' if label else ' '
            crossing = '┼' if label else '┊'
            pixels = []
            for x, cell in enumerate(cells):
                if cell:
                    band = bands[x]
                    # Keep depth in the fill while letting the silhouette reach its measured shade.
                    shade = (2 * band + min(band, level_band)) // 3
                    pixels.append((self.wave_inks[role][shade], cell))
                else:
                    pixels.append((self.colors.get('grid'), crossing if time_ticks[x] == '┬' else background))
            if note:
                x, text = note
                pixels[x:x + len(text)] = [(self.colors.get('ink'), char) for char in text]
            return axis(label, tick) + ''.join(self.tint(''.join(glyph for _, glyph in run), ink)
                                               for ink, run in groupby(pixels, key=lambda pixel: pixel[0]))

        baseline = []
        for x, tick in enumerate(time_ticks):
            pair = series[1][x * stride:(x + 1) * stride]
            if all(value is None for value in pair):
                baseline.append(('total', '┄'))
            elif None in pair:
                baseline.append(('total', '╴' if pair[0] is None else '╶'))
            else:
                baseline.append(('frame', tick))

        legend = self.paint('↓ 下载', 'down') + self.paint('  ↑ 上传', 'up') + self.paint('  Mbps', 'muted')
        if self.colors and graph_width >= 68:
            swatches = [''.join(self.tint('━', ink) for ink in self.wave_inks[role]) for role in ('down', 'up')]
            legend = (self.paint('↓ 下载 ', 'down') + swatches[0] + self.paint('   ↑ 上传 ', 'up') + swatches[1]
                      + self.paint('   低 → 高 · Mbps', 'muted'))
        if any(role == 'total' for role, _ in baseline):
            legend += self.paint('  ┄ 缺样', 'total')
        if totals and totals[2]:
            down_share = totals[1] / totals[2]
            segments = 12
            n = round(segments * down_share)
            mix = (self.paint('流量占比  ', 'muted') + self.paint('↓ {:.0%} '.format(down_share) + '▰' * n, 'down')
                   + self.paint('▱' * (segments - n) + ' ↑ {:.0%}'.format(1 - down_share), 'up'))
            if display_width(legend) + display_width(mix) + 2 <= graph_width:
                legend = pad(legend, graph_width - display_width(mix)) + mix
        # A manually narrowed plot still gets a full-width legend.
        legend_side = min(side, max(0, width - 4 - display_width(legend)))
        for index, role, above in ((1, 'down', True), (0, 'up', False)):
            lane_height = height + int(above and extra_row)
            values = series[index]
            scale = chart_scale(peaks[index], live)
            rows = area_cells(values, lane_height, scale, above=above, smooth=not live)
            inks = self.wave_inks[role]
            # A terminal character has one foreground color even when it holds two samples.
            bands = [round(max(v or 0 for v in values[x:x + stride]) / color_peak * (len(inks) - 1))
                     for x in range(0, len(values), stride)]
            note = peak_label(values, stride, rows, above, start, end, step, zone)
            for i, cells in enumerate(rows):
                level = lane_height - i if above else i + 1
                tick_step = max(2, lane_height // 4) if lane_height >= 16 else max(2, lane_height // 2)
                show_tick = level == lane_height or (lane_height >= 4 and level % tick_step == 0 and level < lane_height - 1)
                label = '{:.3g}'.format(scale * level / lane_height) if show_tick else ''
                if len(label) >= axis_width:
                    label = '{:.2g}'.format(scale * level / lane_height)
                level_band = round(level / lane_height * scale / color_peak * (len(inks) - 1))
                area.append(graph_line(cells, '┤' if show_tick else '│', label, role, bands,
                                       level_band, note[1:] if note and note[0] == i else None))
            if above:
                area.append(axis('0', '┼') + ''.join(self.paint(''.join(glyph for _, glyph in run), role)
                            for role, run in groupby(baseline, key=lambda item: item[0])))
        area.append(' ' * axis_width + self.paint(labels, 'muted'))
        bucket = '{:.2f}s'.format(step) if step < 60 else '{:.1f}m'.format(step / 60)
        interval_label = '每点' if live else '每列'
        lines = [' ' * legend_side + legend] + [' ' * side + line for line in area]
        return self.panel('网速趋势', lines, width=width,
                          detail='{} {}'.format(interval_label, bucket))

    def connection_trend(self, history, inside, trace_rows):
        lines = []
        prefix = '已建立趋势  '
        multi = trace_rows >= 4
        axis_size = 2 + len(str(max((n for n in history if n is not None), default=0)))
        trace_width = max(1, inside - (axis_size if multi else display_width(prefix)))
        recent = history[-trace_width:]
        peak = max((n for n in recent if n is not None), default=0) or 1
        if multi:
            heading = '已建立趋势'
            if recent:
                heading += '  ·  最近 {} 次'.format(len(recent))
            lines.append(self.paint(clip(heading, inside), 'muted'))
            values = [None] * (trace_width - len(recent)) + list(recent)
            cells = area_cells(values, trace_rows - 1, peak, above=True)
            for i, row in enumerate(cells):
                label = str(peak) if i == 0 else '0' if i == len(cells) - 1 else ''
                axis = self.paint(pad(label, axis_size - 1, True) + '│', 'frame')
                trace = ''.join(cell or ' ' for cell in row)
                band = round(7 * (1 - i / (len(cells) - 1)))
                lines.append(axis + self.tint(trace, self.wave_inks['down'][band]))
        else:
            trace = ''.join(' ' if n is None else '▁▂▃▄▅▆▇█'[round(7 * n / peak)] for n in recent)
            lines.append(self.paint(prefix, 'muted') + self.paint(pad(trace, trace_width, right=True), 'down'))
        return lines

    def connection_panel(self, connections, width, budget, page=0):
        if connections is None or budget < 3:
            return [], False
        snapshot, history = connections
        inside = width - 4
        lines = []
        pages = 1
        # Reserve the same rows while the first socket read is in flight and on read errors.
        groups = [('已建立', snapshot.established, 'down'), ('监听', snapshot.listening, 'ink'),
                  ('TIME_WAIT', snapshot.time_wait, 'up'), ('其他', snapshot.other, 'muted')]
        per_row = 4 if inside >= 64 else 2 if inside >= 28 else 1
        cell_width = inside // per_row
        for i in range(0, len(groups), per_row):
            cells = []
            for label, count, role in groups[i:i + per_row]:
                number = str(count)
                name = clip(label, max(0, cell_width - len(number) - 2))
                item = self.paint(name + ' ', 'muted') + self.paint(number, role)
                cells.append(pad(item, cell_width))
            lines.append(''.join(cells))
        if snapshot.status:
            lines = [self.paint(clip(snapshot.status, inside), 'muted')] + [''] * (len(lines) - 1)
        if budget >= 7:
            # History and peer count change on every read. Only the terminal sizes these areas.
            trace_rows = max(1, (budget - len(lines) - 3) // 2)
            lines.extend(self.connection_trend(history, inside, trace_rows))
        room = budget - len(lines) - 3
        if room >= 1 and snapshot.peers:
            pages = (len(snapshot.peers) + room - 1) // room
            offset = (page % pages) * room
            peers = snapshot.peers[offset:offset + room]
            digits = max(4, len(str(peers[0][1])))
            longest = max(len(addr) for addr, _ in peers)
            bars = min(24, max(0, inside - longest - digits - 3))
            if bars < 4:
                bars = 0
            addr_width = inside - digits - 1 - (bars + 1 if bars else 0)
            heading = '已建立对端'
            if pages > 1:
                heading += '  {}–{}/{}'.format(offset + 1, offset + len(peers), len(snapshot.peers))
            lines.append(self.paint(pad(clip(heading, inside - digits), inside - digits)
                                    + pad('连接', digits, True), 'muted'))
            for address, count in peers:
                meter = ''
                if bars:
                    filled = max(1, round(bars * count / snapshot.peers[0][1]))
                    meter = self.paint('━' * filled, 'down') + self.paint('┄' * (bars - filled), 'grid') + ' '
                lines.append(self.paint(pad(clip(address, addr_width), addr_width), 'ink') + ' '
                             + meter + self.paint(pad(str(count), digits, True), 'down'))
        elif room >= 1:
            lines.append(self.paint(pad('已建立对端', inside - 4) + '连接', 'muted'))
            if not snapshot.status:
                lines.append(self.paint('暂无已建立连接', 'muted'))
        lines.extend([''] * max(0, budget - 2 - len(lines)))
        return self.panel('TCP 连接 · 本机', lines[:budget - 2], width=width, role='ink'), pages > 1

    def live_footer(self, view='traffic', paging=False):
        period = '{:g}s'.format(self.interval)
        actions = [('q', '退出', 'total'),
                   ('c', '返回' if view == 'connections' else '连接', 'up'),
                   ('i', '返回' if view == 'ip' else 'IP', 'down'), ('+/-', period, 'ink')]
        keys = '   '.join(self.paint(' ' + key + ' ', role, reverse=True) + ' ' + label
                          for key, label, role in actions)
        if paging:
            keys += '   ' + self.paint(' j/k ', 'ink', reverse=True) + ' 翻页'
        if display_width(keys) > self.columns:
            return clip(period + ' +/-  q 退出  i IP  c 连接' + ('  j/k 翻页' if paging else ''), self.columns)
        spare = self.columns - display_width(keys)
        if spare >= 12:
            keys += '  ' + self.paint('╺' + '─' * (spare - 9) + '╴', 'frame') + self.paint('╱╱', 'up') + self.paint('╱╱╱', 'down')
        return keys

    def live_body(self, report, rates, height, rail, space, connections):
        summary = report.summary
        metrics = self.metrics(summary, rates=rates, big_readout=True, rail=rail)
        if rail and report.chart:
            if len(metrics) + 7 > space:
                metrics = self.metrics(summary, compact=True, rates=rates, big_readout=True, rail=rail)
            sockets, _ = self.connection_panel(connections, rail, space - len(metrics))
            side = metrics + sockets
            wave_rows = space - 5
            graph = self.graph(report.chart, wave_rows // 2, report.zone, True, width=self.columns - rail - 1,
                               extra_row=bool(wave_rows % 2))
            return [pad(card, rail) + ' ' + line for card, line in zip_longest(side, graph, fillvalue='')]

        if metrics and len(metrics) + 13 > space:
            metrics = self.metrics(summary, compact=True, rates=rates, big_readout=space >= 18)
        spare = space - len(metrics)
        if report.chart:
            # At low heights keep the waveform legible. The c key always opens the peer list.
            reserved = min(spare // 3, max(0, spare - 13)) if height is None else spare - 5 - height * 2
            sockets, _ = self.connection_panel(connections, self.columns, reserved)
            wave_rows = max(0, spare - len(sockets) - 5)
            if height is not None:
                wave_rows = min(wave_rows, height * 2)
            if wave_rows >= 2:
                metrics.extend(self.graph(report.chart, wave_rows // 2, report.zone, True,
                                          extra_row=bool(wave_rows % 2)))
                return metrics + sockets
        sockets, _ = self.connection_panel(connections, self.columns, space - len(metrics))
        return metrics + sockets

    def connection_screen(self, connections, rows, page):
        if self.columns < 24 or rows < 4:
            snap = connections[0]
            lines = [clip(snap.status or 'TCP 已建立 {}'.format(snap.established), self.columns)]
            paging = False
        else:
            lines, paging = self.connection_panel(connections, self.columns, rows - 1, page)
        return '\n'.join((lines + [''] * max(0, rows - len(lines) - 1) + [self.live_footer('connections', paging)])[-rows:])

    def ip_screen(self, capture, rows, page):
        width = self.columns - 4
        title = 'IP 流量 · ' + clean(capture.interface)
        status = clean(capture.status) or ('' if capture.rows else '等待 IP 流量')
        if self.columns < 18 or rows < 5:
            lines = [clip(title, self.columns), clip(status or '对端 {}'.format(len(capture.rows)), self.columns)]
            return '\n'.join((lines[:rows - 1] + [''] * max(0, rows - 3) + [self.live_footer('ip')]))

        longest = max((len(p.address) for p in capture.rows), default=0)
        wide = width >= max(67, longest + 52)
        headings = ('↓ 下载速率', '↑ 上传速率', '↓ 累计下载', '↑ 累计上传')
        lines = [self.paint(clip(status, width), 'muted')] if status else []
        address_lines = max(1, (longest + width - 1) // width)
        if wide:
            addr_width = width - 52
            lines.append(self.paint(pad('对端 IP', addr_width) + ''.join(pad(h, 13, True) for h in headings), 'muted'))
        capacity = max(1, (rows - 3 - len(lines)) // (1 if wide else address_lines + 2))
        pages = max(1, (len(capture.rows) + capacity - 1) // capacity)
        offset = (page % pages) * capacity
        for peer in capture.rows[offset:offset + capacity]:
            values = (speed(peer.down_mbps), speed(peer.up_mbps), traffic(peer.download), traffic(peer.upload))
            if wide:
                line = self.paint(pad(peer.address, addr_width), 'ink')
                for value, role in zip(values, ('down', 'up', 'down', 'up')):
                    line += self.paint(pad(clip(value, 12), 13, True), role)
                lines.append(line)
            else:
                lines.extend(self.paint(peer.address[i:i + width], 'ink') for i in range(0, len(peer.address), width))
                for arrow, role, rate, total in (('↓', 'down', values[0], values[2]), ('↑', 'up', values[1], values[3])):
                    lines.append(self.paint(clip('{} {}  ·  累计 {}'.format(arrow, rate, total), width), role))
        detail = '{}/{}'.format(page % pages + 1, pages) if pages > 1 else ''
        lines = lines[:rows - 3]
        lines.extend([''] * (rows - 3 - len(lines)))
        return '\n'.join(self.panel(title, lines, detail=detail) + [self.live_footer('ip', pages > 1)])

    def render(self, report, height, live_sample=None, rows=30, rail=0, connections=None):
        zone = clean(report.zone or '系统时区')
        interface = clean(report.interface)
        summary = report.summary
        live = live_sample is not None
        if self.columns < 34 or (live and rows < 17):
            header = clip('NET MONITOR · ' + interface, self.columns)
            if live:
                measurements = self.live_readings(live_sample, self.columns)
                lines = [header, *measurements]
                if connections and self.columns >= 24 and rows - len(lines) >= 4:
                    sockets, _ = self.connection_panel(connections, self.columns, rows - len(lines) - 1)
                    lines.extend(sockets)
                elif connections and len(lines) + 2 <= rows:
                    snap = connections[0]
                    lines.append(clip('本机 TCP  ' + (snap.status or '已建立 {}'.format(snap.established)), self.columns))
                lines += [''] * max(0, rows - len(lines) - 1)
                return '\n'.join((lines + [self.live_footer()])[-rows:])
            if summary.count:
                output = [label + '  ' + traffic(summary.totals[i]) for label, i, _ in DIRECTIONS]
            else:
                output = ['暂无采样数据']
            return '\n'.join([header, *(clip(line, self.columns) for line in output)])
        title = 'NETWORK MONITOR'
        status = '● 实时' if live else '◇ 历史统计'
        width = self.columns - 4
        stamp = lambda value: local_datetime(value, report.zone).strftime('%Y-%m-%d %H:%M:%S %z')
        first, last = stamp(report.start), stamp(report.end)
        range_text = '查询  {}  →  {}'.format(first, last)
        if display_width(range_text) > width and first[:10] == last[:10] and first[-5:] == last[-5:]:
            range_text = '查询  {} → {}'.format(first[:19], last[11:])
        iface = clip(interface, width - display_width('网卡  '))
        meta = '   ·   时区  ' + zone
        lines = [self.paint('网卡  ', 'muted') + self.tint(iface, self.title_ink)
                 + self.paint(clip(meta, max(0, width - 6 - display_width(iface))), 'muted')]
        if display_width(range_text) <= width:
            lines.append(self.paint(range_text, 'muted'))
        else:
            lines.extend(self.paint(clip(label + '  ' + stamp(value), width), 'muted')
                         for label, value in (('起始', report.start), ('结束', report.end)))
        if live or summary.count:
            interval = '{:.2f}s'.format(summary.duration / summary.count) if summary.count else '—'
            sampling = '采样  {:,} 次  ·  有效  {}  ·  平均间隔  {}'.format(
                summary.count, elapsed(summary.duration), interval)
            if display_width(sampling) <= width:
                lines.append(self.paint(sampling, 'muted'))
            else:
                lines.extend(self.paint(line, 'muted') for line in (
                    '采样  {:,} 次  ·  间隔  {}'.format(summary.count, interval),
                    '有效  ' + elapsed(summary.duration)))
        # Fit the mark alongside the metadata; it never costs a graph row.
        mark = (('█▄  █', '█▄ ▄█'), ('█ ▀▄█', '█ ▀ █'), ('█   █', '█   █'))
        totals = summary.totals
        if len(lines) == len(mark) and all(display_width(line) + 16 <= width for line in lines):
            lines = [self.paint(n, 'down') + ' ' + self.paint(m, 'up') + self.paint('  │  ', 'frame') + line
                     for (n, m), line in zip(mark, lines)]
            segments = 24
            if totals[2] and all(display_width(line) + segments + 2 <= width for line in lines):
                share = totals[1] / totals[2]
                filled = round(segments * share)
                mix = (self.paint('累计流量占比', 'ink'),
                       self.paint('▰' * filled, 'down') + self.paint('▱' * (segments - filled), 'up'),
                       self.paint(pad('↓ {:.0%}'.format(share), segments // 2), 'down')
                       + self.paint(pad('↑ {:.0%}'.format(1 - share), segments // 2, right=True), 'up'))
                lines = [pad(line, width - segments) + panel for line, panel in zip(lines, mix)]
                totals = None
        rates_in_cards = live and self.columns >= 48
        if rates_in_cards and live_sample.rates is None:
            status = '● ' + live_sample.status
        if live and not rates_in_cards:
            lines.extend(self.live_readings(live_sample, width))
        output = self.panel(title, lines, role='down', detail=status, detail_role='total' if live else 'up')
        if live:
            space = rows - len(output) - 1
            rates = (live_sample.rates or (None, None, None)) if rates_in_cards else None
            output.extend(self.live_body(report, rates,
                                         height, rail, space, connections))
            output += [''] * max(0, rows - len(output) - 1)
            return '\n'.join(output + [self.live_footer()])
        if summary.count:
            metrics = self.metrics(summary, big_readout=True, rail=rail)
            if rail and report.chart is not None:
                # The borders, legend, zero line and time labels occupy five rows.
                graph_height, extra_row = divmod(len(metrics) - 5, 2)
                graph = self.graph(report.chart, graph_height, report.zone, live, totals, self.columns - rail - 1,
                                   extra_row=bool(extra_row))
                output.extend(pad(card, rail) + ' ' + line for card, line in zip_longest(metrics, graph, fillvalue=''))
            else:
                output.extend(metrics)
            if not rail and report.chart is not None:
                available = (rows - len(output) - 5) // 2
                graph_height = height or min(8, max(3, available))
                if graph_height:
                    output.extend(self.graph(report.chart, graph_height, report.zone, live, totals))
        else:
            output.extend(self.panel('暂无采样数据', ['此网卡在所选时间范围内没有记录。']
                                     if width >= 32 else ['所选时段内没有记录。']))
        return '\n'.join(output)


def chart_scale(peak, live=False):
    if not peak:
        return 1.0
    if live:
        return peak * 1.25
    order = 10 ** math.floor(math.log10(peak))
    return next(order * value for value in (1, 2, 2.5, 5, 10) if order * value >= peak)


def peak_label(values, stride, rows, above, start, end, step, zone):
    """Attach the weighted bucket rate and local interval to a free edge of the peak."""
    if len(rows) < 3:
        return None
    peak = max((v for v in values if v is not None), default=0)
    if not peak:
        return None
    index = values.index(peak)
    x = index // stride
    order = range(len(rows)) if above else reversed(range(len(rows)))
    edge = next(y for y in order if rows[y][x])
    outer = edge - 1 if above else edge + 1
    arrow = '▾' if above else '▴'
    first, last = local_datetime(start, zone), local_datetime(end, zone)
    fmt = '%H:%M:%S' if first.date() == last.date() else '%m-%d %H:%M:%S'
    if step < 1:
        fmt += '.%f'
    if first.utcoffset() != last.utcoffset():
        fmt += ' %z'
    interval = '–'.join(local_datetime(min(end, start + i * step), zone).strftime(fmt)
                        for i in (index, index + 1))
    rate = speed(peak)
    for label in (rate + ' · ' + interval, rate):
        positions = ((outer, x, arrow + ' ' + label), (outer, x - len(label) - 1, label + ' ' + arrow),
                     (edge, x + 1, '◂ ' + label), (edge, x - len(label) - 2, label + ' ▸'))
        for y, left, text in positions:
            right = left + len(text)
            if 0 <= y < len(rows) and 0 <= left and right <= len(rows[y]) and not any(rows[y][left:right]):
                return y, left, text
    return None


def area_cells(values, height, limit, above, smooth=True):
    # Live charts pack two independent buckets into each Braille cell.
    # Historical charts use both halves of the cell for a smooth transition.
    dots = ((1, 2, 4, 64), (8, 16, 32, 128))
    columns = len(values) if smooth else (len(values) + 1) // 2
    rows = [[''] * columns for _ in range(height)]
    for column in range(columns):
        if smooth:
            value = values[column]
            if value is None:
                continue
            previous = values[column - 1] if column else None
            leading = (previous + value) / 2 if previous is not None else value
            points = (leading, value)
        else:
            points = values[2 * column:2 * column + 2]
        levels = [0 if point is None else max(1, min(height * 4, round(point / limit * height * 4)))
                  for point in points]
        for row in range(height):
            offset = (height - row - 1 if above else row) * 4
            cell = 0
            for x, level in enumerate(levels):
                for y in range(min(4, max(0, level - offset))):
                    cell |= dots[x][3 - y if above else y]
            if cell:
                rows[row][column] = chr(0x2800 + cell)
    return rows


def time_labels(start, end, columns, zone):
    left, right = local_datetime(start, zone), local_datetime(end, zone)
    if left.date() != right.date():
        fmt = '%m-%d %H:%M'
    elif right.timestamp() - left.timestamp() < 120:
        fmt = '%H:%M:%S'
    else:
        fmt = '%H:%M'
    if left.utcoffset() != right.utcoffset():
        fmt += ' %z'
    size = len(left.strftime(fmt))
    ticks = ['─'] * columns
    labels = [' '] * columns
    count = min(6, max(1, (columns + 2) // (size + 2)))
    for i in range(count):
        fraction = i / (count - 1) if count > 1 else 0
        position = round(fraction * (columns - 1))
        label = local_datetime(start + fraction * (end - start), zone).strftime(fmt)[:columns]
        offset = max(0, min(columns - len(label), position - len(label) // 2))
        labels[offset:offset + len(label)] = label
        ticks[position] = '┬'
    return ''.join(ticks), ''.join(labels)
