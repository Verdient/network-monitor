"""Render a terminal dashboard without terminal or database dependencies."""

from bisect import bisect_left
from dataclasses import dataclass
from ipaddress import ip_address
from itertools import groupby, zip_longest
import math
import re
import unicodedata

from time_ranges import local_datetime, timezone_label


ANSI = re.compile(r'\x1b\[[0-9;]*m')
BRAILLE_DOTS = ((1, 2, 4, 64), (8, 16, 32, 128))
COLORS = {
    'frame': '38;2;105;102;153', 'grid': '38;2;81;85;112', 'muted': '38;2;169;175;201',
    'ink': '1;38;2;238;244;255', 'down': '1;38;2;79;248;238',
    'up': '1;38;2;255;105;203', 'total': '1;38;2;255;207;107',
    'peer_location': '38;2;204;210;231', 'peer_carrier': '38;2;169;175;201',
    'peer_asn': '38;2;156;162;184', 'peer_name': '38;2;139;145;168',
}
BASIC = {
    'frame': '90', 'grid': '90', 'muted': '37', 'ink': '1;37', 'down': '1;36',
    'up': '1;35', 'total': '1;33',
    'peer_location': '39', 'peer_carrier': '2;39', 'peer_asn': '90', 'peer_name': '2;90',
}
LIGHT_COLORS = {
    'frame': ((75, 75, 108), 60), 'grid': ((95, 95, 119), 59),
    'down': ((0, 87, 117), 24), 'up': ((135, 0, 95), 89), 'total': ((135, 95, 0), 94),
    'peer_location': ((66, 71, 85), 238), 'peer_carrier': ((95, 100, 115), 241),
    'peer_asn': ((105, 110, 125), 242), 'peer_name': ((116, 120, 133), 243),
}
LIGHT_WAVES = {
    'down': (((0, 126, 148), 31), ((0, 81, 107), 24)),
    'up': (((165, 79, 128), 132), ((123, 26, 83), 89)),
}
PORTABLE_COLORS = {
    'frame': ((123, 119, 132), 244), 'grid': ((119, 119, 126), 243),
    'down': ((0, 135, 140), 30), 'up': ((188, 76, 133), 132), 'total': ((171, 105, 0), 130),
    'peer_carrier': ((142, 145, 159), 246), 'peer_asn': ((130, 133, 145), 244),
    'peer_name': ((117, 117, 128), 243),
}
PORTABLE_WAVES = {
    'down': (((0, 125, 135), 30), ((0, 142, 163), 31)),
    'up': (((166, 78, 123), 132), ((194, 82, 145), 133)),
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
IP_COLUMNS = (
    ('address', '对端 IP', 'IP'),
    ('down_mbps', '↓ 下载速率', '↓速'), ('up_mbps', '↑ 上传速率', '↑速'),
    ('download', '↓ 累计下载', '↓量'), ('upload', '↑ 累计上传', '↑量'),
)
CARRIER_ALIASES = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r'中国移动|\b(?:China[ -]*Mobile|CMNET|CMCC)\b', '中国移动'),
        (r'中国联通|\b(?:China[ -]*Unicom|China United Network Communications|China169|CUCC)\b', '中国联通'),
        (r'中国电信|\b(?:China[ -]*Telecom|ChinaNet)\b', '中国电信'),
        (r'中国广电|\bChina[ -]*Broadnet\b', '中国广电'),
        (r'中国教育和科研计算机网|\bCERNET\b', '教育网'),
        (r'阿里云|\b(?:Aliyun|Alibaba (?:Cloud|\(?US\)? Technology|Advertising))\b', '阿里云'),
        (r'腾讯云|\bTencent[ -]*Cloud\b', '腾讯云'),
        (r'腾讯|\bTencent\b', '腾讯'),
        (r'华为云|\bHuawei[ -]*Cloud\b', '华为云'),
    )
)
SINGLE_PAGE = (0, 1)
# Three terminal rows encode a five-pixel-high digit using half blocks.
DIGITS = {
    '0': ('█▀█', '█ █', '▀▀▀'), '1': ('▄█ ', ' █ ', '▀▀▀'),
    '2': ('▀▀█', '█▀▀', '▀▀▀'), '3': ('▀▀█', '▀▀█', '▀▀▀'),
    '4': ('█ █', '▀▀█', '  ▀'), '5': ('█▀▀', '▀▀█', '▀▀▀'),
    '6': ('█▀▀', '█▀█', '▀▀▀'), '7': ('▀▀█', ' █ ', ' ▀ '),
    '8': ('█▀█', '█▀█', '▀▀▀'), '9': ('█▀█', '▀▀█', '▀▀▀'),
    '.': (' ', ' ', '▀'),
}
BLOCK_MARK = (('█▄  █', '█▄ ▄█'), ('█ ▀▄█', '█ ▀ █'), ('█   █', '█   █'))


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


def carrier_label(name):
    text = re.sub(r'[\s_]+', ' ', name)
    return next((label for pattern, label in CARRIER_ALIASES if pattern.search(text)), name)


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
    # Leave room for three full-size cards, the header, and live controls.
    rail = 36
    if columns >= 112 and rows >= 34 and requested_height is None:
        if requested_width is None or requested_width <= columns - rail - 1 - 12:
            return rail
    return 0


def ip_preview_fits(columns, rows, rail):
    return rows >= 36 and columns - rail - bool(rail) >= 80


def ip_preview_budget(space, height):
    budget = min(space // 3, space - 5 - 2 * (height or 4))
    return budget if budget >= 7 else 0


class Dashboard:
    def __init__(self, columns, color=False, truecolor=False, indexed=False, interval=1.0,
                 theme=None, zone=None):
        self.columns = columns
        self.interval = interval
        self.colors = (COLORS if truecolor else BASIC) if color else {}
        self.truecolor = color and truecolor
        self.zone = zone
        self.paging = SINGLE_PAGE
        self.title_ink = ''
        if color:
            if theme != 'dark':
                accents = LIGHT_COLORS if theme == 'light' else PORTABLE_COLORS
                self.colors = {role: ('1;39' if role == 'ink' else '39') for role in BASIC}
                for role, (rgb, index) in accents.items():
                    self.colors[role] = self.palette_color(rgb, index, indexed)
                if not (self.truecolor or indexed):
                    for role in ('peer_location', 'peer_carrier', 'peer_asn', 'peer_name'):
                        self.colors[role] = BASIC[role]
                self.title_ink = self.colors['ink']
            elif self.truecolor:
                self.title_ink = '1;38;2;247;250;255'
            elif indexed:
                self.title_ink = '1;38;5;231'
            else:
                self.title_ink = '1;97'
        self.wave_inks = {}
        for role, stops in WAVE_SHADES.items():
            if not color:
                inks = [''] * len(stops)
            elif theme != 'dark':
                waves = LIGHT_WAVES if theme == 'light' else PORTABLE_WAVES
                low, high = (self.palette_color(rgb, index, indexed) for rgb, index in waves[role])
                inks = [low] * 4 + [high] * 4
            elif self.truecolor or indexed:
                inks = [self.palette_color(rgb, index, indexed) for rgb, index in stops]
            else:
                normal, bright = ('36', '96') if role == 'down' else ('35', '95')
                inks = [normal] * 4 + [bright] * 4
            self.wave_inks[role] = inks

    def palette_color(self, rgb, index, indexed):
        if self.truecolor:
            return '38;2;{};{};{}'.format(*rgb)
        return '38;5;{}'.format(index) if indexed else '39'

    @staticmethod
    def tint(text, code):
        return '\033[{}m{}\033[0m'.format(code, text) if code and text else text

    def paint(self, text, role, bold=False, reverse=False):
        code = self.colors.get(role)
        if code and bold and not code.startswith('1;'):
            code = '1;' + code
        if code and reverse:
            code += ';7'
        return self.tint(text, code)

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

    def panel(self, title, lines, width=None, role='ink', detail='', detail_role='muted', accented=False):
        width = self.columns if width is None else width
        frame = lambda text: self.paint(text, 'frame')
        title = ' {} '.format(clip(title, width - 8))
        room = width - 6 - display_width(title)
        trailer = ' {} '.format(detail) if detail and display_width(detail) + 3 <= room else ''
        border_width = room - display_width(trailer)
        accent = ''
        if accented and border_width >= 32:
            accent = self.paint('━╸', 'down') + self.paint('╱╱', 'up') + frame('──')
        top = (self.paint('┏━', role) + self.tint(title, self.title_ink) + self.paint('╲', role)
               + frame('─' * (border_width - display_width(accent))) + accent + self.paint(trailer, detail_role)
               + self.paint('╱╱┓', role))
        body = [frame('│') + ' ' + pad(line, width - 4) + ' ' + frame('│') for line in lines]
        bottom = frame('└' + '─' * (width - 4)) + self.paint('╴╱┘', role)
        return [top, *body, bottom]

    def digits(self, reading, role, width):
        number, _, unit = reading.replace(',', '').partition(' ')
        if not unit or any(char not in DIGITS for char in number):
            return []
        rows = [' '.join(DIGITS[char][y] for char in number) for y in range(3)]
        if len(rows[0]) + len(unit) + 1 > width:
            return []
        lines = [self.paint(row, role) for row in rows]
        lines[-1] += self.paint(' ' + unit, 'ink')
        return lines

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

        if self.columns >= 78:
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
                    primary = not lines
                    lines.append(self.paint(key, 'muted') + self.paint(value, role if primary else 'ink', bold=primary))
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
        title = '已建立趋势'
        peak = max((count for _, count in history if count is not None), default=None)
        summary = '峰值 {}'.format(peak) if peak is not None else ''
        multi = trace_rows >= 4
        limit = math.ceil(chart_scale(peak, live=True))
        ceiling = str(limit)
        prefix = title + '  '
        if not multi and summary and display_width(prefix + summary) + 3 <= inside:
            prefix += summary + '  '
        axis_size = 2 + len(ceiling)
        trace_width = max(1, inside - (axis_size if multi else display_width(prefix)))
        columns = connection_columns(history, trace_width)
        values = [sample[1] if sample is not None else None for sample in columns]
        traces = line_cells(values, trace_rows - 1 if multi else 1, limit)
        if not multi:
            return [self.paint(prefix, 'muted') + self.paint(''.join(cell or ' ' for cell in traces[0]), 'down')]

        heading = title
        if history:
            heading += '  ·  {} 次'.format(len(history))
        note = None
        if peak is not None:
            column = values.index(peak)
            stamp = local_datetime(columns[column][0], self.zone)
            start = local_datetime(history[0][0], self.zone)
            end = local_datetime(history[-1][0], self.zone)
            fmt = '%H:%M:%S' if start.date() == end.date() else '%m-%d %H:%M:%S'
            if start.utcoffset() != end.utcoffset():
                fmt += ' %z'
            detail = summary + ' · ' + stamp.strftime(fmt)
            edge = next(row for row, trace in enumerate(traces) if trace[column])
            note = place_peak_note(traces, column, edge, True, (detail, summary))
            if note is None:
                heading = next((text for text in (title + '  ·  ' + detail, title + '  ' + summary)
                                if display_width(text) <= inside), summary)

        lines = [self.paint(clip(heading, inside), 'muted')]
        for i, trace in enumerate(traces):
            label = ceiling if i == 0 else '0' if i == len(traces) - 1 else ''
            axis = self.paint(pad(label, axis_size - 1, True) + '│', 'frame')
            text = ''.join(cell or ' ' for cell in trace)
            if note and note[0] == i:
                _, left, annotation = note
                right = left + display_width(annotation)
                lines.append(axis + self.paint(text[:left], 'down') + self.paint(annotation, 'total')
                             + self.paint(text[right:], 'down'))
            else:
                lines.append(axis + self.paint(text, 'down'))
        return lines

    def connection_panel(self, connections, width, budget, page=0):
        if connections is None or budget < 3:
            return [], SINGLE_PAGE
        snapshot, history = connections
        inside = width - 4
        lines = []
        paging = SINGLE_PAGE
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
            page = min(max(page, 0), pages - 1)
            paging = page, pages
            offset = page * room
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
        return self.panel('TCP 连接 · 本机', lines[:budget - 2], width=width, role='ink'), paging

    def paint_button(self, text, role, enabled=True):
        if enabled:
            return self.paint(text, role, bold=True, reverse=True)
        color = self.colors.get('frame')
        return self.tint(text, '2;' + color if color else '')

    def live_footer(self, row, view, paging, click_targets):
        page, page_count = paging
        period = '{:g}s'.format(self.interval)
        navigation = [('q', '退出', 'total'), ('c', '返回' if view == 'connections' else '连接', 'up'),
                      ('i', '返回' if view == 'ip' else 'IP', 'down')]
        pages = [('p', '上页', 'ink'), ('n', '下页', 'ink')] if view == 'connections' and page_count > 1 else []
        disabled = {key for key, blocked in (('n', page == page_count - 1), ('p', page == 0)) if blocked}
        interval = [('-', '', 'ink'), (None, period, 'muted'), ('+', '', 'ink')]
        links = navigation + pages
        label_width = sum(1 + display_width(label) for _, label, _ in links)
        # The interval uses one space on either side of the reading.
        full_width = len(period) + 8 + 6 * len(links) + label_width
        compact = full_width > self.columns
        actions = interval + links if compact else navigation + interval + pages
        button_width = 3
        show_labels = True
        if compact:
            button_count = len(actions) - 1
            minimum_width = len(period) + 2 * button_count
            if minimum_width + 2 * button_count > self.columns:
                button_width = 1
            show_labels = minimum_width + (button_width - 1) * button_count + label_width <= self.columns

        parts = []
        left = 0
        for key, label, role in actions:
            gap = 0 if not parts else 1 if compact or key in (None, '+') else 3
            left += gap
            if key is None:
                text = self.paint(clip(label, self.columns - left), role)
            else:
                symbol = '·' if key in disabled and not self.colors else key
                button = symbol + (' ' + label if show_labels and label else '')
                if button_width == 3:
                    button = ' ' + button + ' '
                text = self.paint_button(button, role, enabled=key not in disabled)
            right = left + display_width(text)
            if right > self.columns:
                break
            parts.append(' ' * gap + text)
            if key is not None and key not in disabled and click_targets is not None:
                click_targets.append((row, left, right, key))
            left = right
        keys = ''.join(parts)
        spare = self.columns - display_width(keys)
        if spare >= 12:
            keys += '  ' + self.paint('╺' + '─' * (spare - 9) + '╴', 'frame') + self.paint('╱╱', 'up') + self.paint('╱╱╱', 'down')
        return keys

    def live_screen(self, lines, rows, view='traffic', paging=SINGLE_PAGE, click_targets=None):
        self.paging = paging
        body = lines[:rows - 1]
        body.extend([''] * (rows - 1 - len(body)))
        return '\n'.join(body + [self.live_footer(rows - 1, view, paging, click_targets)])

    def live_body(self, report, rates, height, rail, space, connections, *, ip_capture=None,
                  ip_page=0, ip_sort=None, click_targets=None, row_offset=0):
        def append_ip(graph, width, budget, left, top):
            if not budget:
                return graph, SINGLE_PAGE
            headers = [] if click_targets is not None else None
            panel, paging = self.ip_panel(ip_capture, width, budget, ip_page, ip_sort, headers)
            if click_targets is not None:
                offset = row_offset + top + len(graph)
                click_targets.extend((row + offset, lo + left, hi + left, column) for row, lo, hi, column in headers)
            return graph + panel, paging

        summary = report.summary
        metrics = self.metrics(summary, rates=rates, big_readout=True, rail=rail)
        if rail and report.chart:
            if len(metrics) + 7 > space:
                metrics = self.metrics(summary, compact=True, rates=rates, big_readout=True, rail=rail)
            sockets, _ = self.connection_panel(connections, rail, space - len(metrics))
            side = metrics + sockets
            width = self.columns - rail - 1
            budget = ip_preview_budget(space, height) if ip_capture is not None else 0
            wave_rows = space - 5 - budget
            graph = self.graph(report.chart, wave_rows // 2, report.zone, True, width=width,
                               extra_row=bool(wave_rows % 2))
            right, paging = append_ip(graph, width, budget, rail + 1, 0)
            return [pad(card, rail) + ' ' + line for card, line in zip_longest(side, right, fillvalue='')], paging

        if metrics and len(metrics) + 13 > space:
            metrics = self.metrics(summary, compact=True, rates=rates, big_readout=space >= 18)
        spare = space - len(metrics)
        if report.chart:
            budget = ip_preview_budget(spare, height) if ip_capture is not None else 0
            spare -= budget
            # At low heights keep the waveform legible. The c key always opens the peer list.
            reserved = min(spare // 3, max(0, spare - 13)) if height is None else spare - 5 - height * 2
            sockets, _ = self.connection_panel(connections, self.columns, reserved)
            wave_rows = max(0, spare - len(sockets) - 5)
            if height is not None:
                wave_rows = min(wave_rows, height * 2)
            if wave_rows >= 2:
                graph = self.graph(report.chart, wave_rows // 2, report.zone, True, extra_row=bool(wave_rows % 2))
                graphs, paging = append_ip(graph, self.columns, budget, 0, len(metrics))
                return metrics + graphs + sockets, paging
        sockets, _ = self.connection_panel(connections, self.columns, space - len(metrics))
        return metrics + sockets, SINGLE_PAGE

    def connection_screen(self, connections, rows, page, click_targets=None):
        if self.columns < 24 or rows < 4:
            snap = connections[0]
            lines = [clip(snap.status or 'TCP 已建立 {}'.format(snap.established), self.columns)]
            paging = SINGLE_PAGE
        else:
            lines, paging = self.connection_panel(connections, self.columns, rows - 1, page)
        return self.live_screen(lines, rows, 'connections', paging, click_targets)

    def ip_screen(self, capture, rows, page, sort=None, click_targets=None):
        lines, paging = self.ip_panel(capture, self.columns, rows - 1, page, sort, click_targets)
        return self.live_screen(lines, rows, 'ip', paging, click_targets)

    def ip_page_controls(self, columns, row, page, pages, click_targets):
        width = columns - 4
        detail = ' {:>{}}/{} '.format(page + 1, len(str(pages)), pages)
        labels = ('p 上页', 'n 下页')
        if display_width(detail) + sum(display_width(label) + 2 for label in labels) > width:
            labels = ('‹', '›')
        padding = ' ' if display_width(detail) + 6 <= width else ''
        button_width = display_width(labels[0]) + 2 * len(padding)
        detail = clip(detail, width - 2 * button_width)
        detail_width = display_width(detail)
        spare = width - 2 * button_width - detail_width
        left = spare // 2
        buttons = []
        for key, label, enabled, start in (('p', labels[0], page > 0, left + 1),
                                           ('n', labels[1], page < pages - 1,
                                            left + 1 + button_width + detail_width)):
            if not enabled and not self.colors:
                label = '·' + label[1:]
            buttons.append(self.paint_button(padding + label + padding, 'ink', enabled))
            if enabled and click_targets is not None:
                click_targets.append((row, start, start + button_width, key))
        previous, following = buttons
        return (self.paint('└' + '─' * left, 'frame') + previous + self.paint(detail, 'muted') + following
                + self.paint('─' * (spare - left), 'frame') + self.paint('╴╱┘', 'ink'))

    def ip_address_line(self, address, fields, width):
        text = self.paint(address, 'ink')
        remaining = width - display_width(address)
        for value, role in zip(fields, ('peer_location', 'peer_carrier', 'peer_asn', 'peer_name')):
            space = '  ' if text else ''
            room = remaining - len(space)
            if not value or room < 3:
                continue
            if role == 'peer_asn' and len(value) > room:
                continue
            if role == 'peer_carrier':
                asn, hostname = fields[2:]
                if asn and room >= len(asn) + 5:
                    room -= len(asn) + 2
                if hostname and room >= 24:
                    room -= min(12, display_width(hostname)) + 2
            value = clip(clean(value), room)
            remaining -= len(space) + display_width(value)
            text += self.paint(space + value, role)
        return text

    def ip_panel(self, capture, columns, budget, page, sort=None, click_targets=None):
        width = columns - 4
        title = 'IP 流量 · ' + clean(capture.interface)
        peers = capture.rows
        status = clean(capture.status) or ('' if peers else '等待 IP 流量')
        if columns < 18 or budget < 4:
            return [clip(title, columns), clip(status or '对端 {}'.format(len(peers)), columns)], SINGLE_PAGE

        if sort:
            column, descending = sort
            if column == 'address':
                def address_key(peer):
                    addr = ip_address(peer.address)
                    return addr.version, int(addr)

                peers = sorted(peers, key=address_key, reverse=descending)
            else:
                direction = -1 if descending else 1
                peers = sorted(peers, key=lambda peer: (direction * getattr(peer, column), peer.address))

        longest = max((len(peer.address) for peer in peers), default=0)
        wide = width >= max(67, longest + 52)
        addr_width = width - 52 if wide else width
        details_for = getattr(capture, 'peer_details', None)
        # A fixed second row gives the labels room without repaging when lookups complete.
        separate_details = details_for is not None and longest + 50 > addr_width
        # The compact IP header needs four cells and the other headers need five.
        per_row = len(IP_COLUMNS) if wide else (width + 1) // 5
        lines = []
        for group in range(0, len(IP_COLUMNS), per_row):
            left = 2  # The panel border and its inside padding precede the header.
            cells = []
            for column, label, short in IP_COLUMNS[group:group + per_row]:
                selected = sort is not None and sort[0] == column
                indicator = ('▾' if sort[1] else '▴') if selected else ' '
                cell_width = (addr_width if column == 'address' else 13) if wide else display_width(short) + 2
                text = '{} {}'.format(label if wide else short, indicator)
                cells.append(self.paint(pad(text, cell_width, right=wide and column != 'address'),
                                        'ink' if selected else 'muted'))
                if click_targets is not None:
                    click_targets.append((len(lines) + 1, left, left + cell_width, column))
                left += cell_width
            lines.append(''.join(cells))
        if status and len(lines) < budget - 2:
            lines.append(self.paint(clip(status, width), 'muted'))
        address_lines = max(1, (longest + addr_width - 1) // addr_width)
        peer_rows = address_lines + (0 if wide else 2) + separate_details
        capacity = max(1, (budget - 2 - len(lines)) // peer_rows)
        pages = max(1, (len(peers) + capacity - 1) // capacity)
        page = min(max(page, 0), pages - 1)
        offset = page * capacity
        hostname_for = getattr(capture, 'hostname', None)
        for peer in peers[offset:offset + capacity]:
            hostname = hostname_for(peer.address) if hostname_for is not None else ''
            location, carrier, asn = details_for(peer.address) if details_for is not None else ('', '', '')
            location = re.sub(r'州(?=·美国$)', '', location)
            fields = (location, carrier_label(carrier), asn, hostname)
            inline_fields = () if separate_details else fields
            values = (speed(peer.down_mbps), speed(peer.up_mbps), traffic(peer.download), traffic(peer.upload))
            if wide:
                line = pad(self.ip_address_line(peer.address, inline_fields, addr_width), addr_width)
                for value, role in zip(values, ('down', 'up', 'down', 'up')):
                    line += self.paint(pad(clip(value, 12), 13, True), role)
                lines.append(line)
            else:
                address = [peer.address[i:i + width] for i in range(0, len(peer.address), width)]
                lines.extend(self.paint(part, 'ink') for part in address[:-1])
                lines.append(self.ip_address_line(address[-1], inline_fields, width))
            if separate_details:
                lines.append(self.ip_address_line('', fields, width))
            if not wide:
                for arrow, role, rate, total in (('↓', 'down', values[0], values[2]), ('↑', 'up', values[1], values[3])):
                    lines.append(self.paint(clip('{} {}  ·  累计 {}'.format(arrow, rate, total), width), role))
        lines = lines[:budget - 2]
        lines.extend([''] * (budget - 2 - len(lines)))
        panel = self.panel(title, lines, width=columns)
        if pages > 1:
            panel[-1] = self.ip_page_controls(columns, budget - 1, page, pages, click_targets)
        return panel, (page, pages)

    def traffic_share(self, totals, width):
        if not totals[2]:
            return ['', '']
        share = totals[1] / totals[2]
        filled = round(width * share)
        return [self.paint('▰' * filled, 'down') + self.paint('▱' * (width - filled), 'up'),
                self.paint(pad('↓ {:.0%}'.format(share), width // 2), 'down')
                + self.paint(pad('↑ {:.0%}'.format(1 - share), width - width // 2, right=True), 'up')]

    def wide_traffic_header(self, report, interface, zone, bounds, mark):
        first, last = bounds
        context = [('网卡  ', interface), ('时区  ', zone), ('起始  ', first), ('结束  ', last),
                   ('查询时长  ', elapsed(report.end - report.start))]
        mark_width = display_width(mark[0])
        logo_width = mark_width + 3
        divider = ' │ '
        available = self.columns - 4 - 2 * display_width(divider)
        context_width = logo_width + max(display_width(label + value) for label, value in context)
        remaining = available - context_width
        if remaining < 48:
            return []
        sampling_width = min(32, remaining // 2)
        flow_width = min(32, remaining - sampling_width)
        context_width += remaining - flow_width - sampling_width
        logos = [self.paint('╶━━', 'down') + self.paint('╱╱', 'up'), *mark,
                 ' ' * (mark_width - 5) + self.paint('╱╱', 'down') + self.paint('━━╴', 'up')]
        context = [pad(logo, logo_width) + self.paint(label, 'muted') + self.paint(value, 'ink')
                   for logo, (label, value) in zip(logos, context)]

        def reading(label, value, size, role='ink'):
            label_width = 8  # Four full-width characters fit the longest labels.
            room = size - label_width
            # Allow a date and time in the value field even when a leader fits.
            leader_width = min(8, room - 14 - 2)
            space = ' '
            if leader_width > 0:
                space += self.paint('┄' * leader_width, 'grid') + ' '
            value_width = room - display_width(space)
            value = pad(clip(value, value_width), value_width, right=True)
            return self.paint(pad(label, label_width), 'muted') + space + self.paint(value, role)

        summary = report.summary
        up, down, total = [traffic(value) if summary.count else '—' for value in summary.totals]
        flow = [reading('累计流量', total, flow_width, 'total'),
                reading('↓ 下载', down, flow_width, 'down'), reading('↑ 上传', up, flow_width, 'up'),
                *self.traffic_share(summary.totals, flow_width)]
        sample_format = '%H:%M:%S' if first[:10] == last[:10] else '%m-%d %H:%M:%S'
        samples = [reading('采样', '{:,} 次'.format(summary.count), sampling_width),
                   reading('有效时长', elapsed(summary.duration) if summary.count else '—', sampling_width),
                   reading('平均间隔', '{:.2f}s'.format(summary.duration / summary.count) if summary.count else '—',
                           sampling_width)]
        for label, timestamp in (('首条采样', summary.first), ('末条采样', summary.last)):
            value = local_datetime(timestamp, report.zone).strftime(sample_format) if timestamp is not None else '—'
            samples.append(reading(label, value, sampling_width))

        divider = self.paint(divider, 'frame')
        return [divider.join((pad(left, context_width), pad(middle, flow_width), right))
                for left, middle, right in zip(context, flow, samples)]

    def render(self, report, height, live_sample=None, rows=30, rail=0, connections=None, click_targets=None,
               *, ip_capture=None, ip_page=0, ip_sort=None):
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
                return self.live_screen(lines, rows, click_targets=click_targets)
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
        mark = [self.paint(n, 'down') + ' ' + self.paint(m, 'up') for n, m in BLOCK_MARK]
        divider = ' │ '
        mark_width = display_width(mark[0]) + display_width(divider)
        range_text = '查询  {}  →  {}'.format(first, last)
        range_width = width - mark_width if self.columns >= 78 else width
        if display_width(range_text) > range_width and first[-5:] == last[-5:]:
            right_stamp = last[11:] if first[:10] == last[:10] else last
            range_text = '查询  {} → {}'.format(first[:19], right_stamp)
        iface = clip(interface, width - display_width('网卡  '))
        zone_name = clean(timezone_label(report.zone))
        zone = '时区  ' + zone_name
        meta = '   ·   ' + zone
        lines = [self.paint('网卡  ', 'muted') + self.tint(iface, self.title_ink)]
        if display_width(lines[0] + meta) <= width:
            lines[0] += self.paint(meta, 'muted')
        else:
            lines.append(self.paint(clip(zone, width), 'muted'))
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
        totals = summary.totals
        wide_header = []
        if self.columns >= 112 and rows >= 42:
            wide_header = self.wide_traffic_header(report, interface, zone_name, (first, last), mark)
        if wide_header:
            lines, totals = wide_header, None
        elif len(lines) == len(mark) and all(display_width(line) + mark_width <= width for line in lines):
            divider = self.paint(divider, 'frame')
            lines = [logo + divider + line for logo, line in zip(mark, lines)]
            segments = min(36, width - max(map(display_width, lines)) - 2)
            if totals[2] and segments >= 24:
                label = '累计流量  '
                heading = self.paint(label, 'ink') + self.paint(clip(traffic(totals[2]), segments - display_width(label)),
                                                                'total')
                mix = [heading, *self.traffic_share(totals, segments)]
                lines = [pad(line, width - segments) + panel for line, panel in zip(lines, mix)]
                totals = None
        rates_in_cards = live and self.columns >= 48
        if rates_in_cards and live_sample.rates is None:
            status = '● ' + live_sample.status
        if live and not rates_in_cards:
            lines.extend(self.live_readings(live_sample, width))
        output = self.panel(title, lines, role='down', detail=status, detail_role='total' if live else 'up',
                            accented=True)
        if live:
            space = rows - len(output) - 1
            rates = (live_sample.rates or (None, None, None)) if rates_in_cards else None
            if (ip_capture is None or not ip_capture.ready or ip_capture.status
                    or not ip_preview_fits(self.columns, rows, rail)):
                ip_capture = None
            body, paging = self.live_body(report, rates, height, rail, space, connections, ip_capture=ip_capture,
                                          ip_page=ip_page, ip_sort=ip_sort, click_targets=click_targets,
                                          row_offset=len(output))
            return self.live_screen(output + body, rows, paging=paging, click_targets=click_targets)
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
    first, last = local_datetime(start, zone), local_datetime(end, zone)
    fmt = '%H:%M:%S' if first.date() == last.date() else '%m-%d %H:%M:%S'
    if step < 1:
        fmt += '.%f'
    if first.utcoffset() != last.utcoffset():
        fmt += ' %z'
    interval = '–'.join(local_datetime(min(end, start + i * step), zone).strftime(fmt)
                        for i in (index, index + 1))
    rate = speed(peak)
    return place_peak_note(rows, x, edge, above, (rate + ' · ' + interval, rate))


def place_peak_note(rows, column, edge, above, labels):
    outer = edge - 1 if above else edge + 1
    arrow = '▾' if above else '▴'
    for label in labels:
        width = display_width(label)
        positions = ((outer, column, arrow + ' ' + label), (outer, column - width - 1, label + ' ' + arrow),
                     (edge, column + 1, '◂ ' + label), (edge, column - width - 2, label + ' ▸'))
        for y, left, text in positions:
            right = left + display_width(text)
            if 0 <= y < len(rows) and 0 <= left and right <= len(rows[y]) and not any(rows[y][left:right]):
                return y, left, text
    return None


def area_cells(values, height, limit, above, smooth=True):
    # Live charts pack two independent buckets into each Braille cell.
    # Historical charts use both halves of the cell for a smooth transition.
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
                    cell |= BRAILLE_DOTS[x][3 - y if above else y]
            if cell:
                rows[row][column] = chr(0x2800 + cell)
    return rows


def line_cells(values, height, limit):
    """Draw a line at Braille dot resolution, with one cell per sample."""
    rows = [[0] * len(values) for _ in range(height)]
    bottom = height * 4 - 1
    previous = None
    for column, value in enumerate(values):
        if value is None:
            previous = None
            continue
        x, y = column * 2 + 1, bottom - round(value / limit * bottom)
        previous_x, previous_y = previous if previous is not None else (x, y)
        dx, dy = x - previous_x, y - previous_y
        steps = max(1, dx, abs(dy))
        for step in range(steps + 1):
            dot_x = previous_x + round(dx * step / steps)
            dot_y = previous_y + round(dy * step / steps)
            rows[dot_y // 4][dot_x // 2] |= BRAILLE_DOTS[dot_x % 2][dot_y % 4]
        previous = x, y
    return [[chr(0x2800 + cell) if cell else '' for cell in row] for row in rows]


def connection_columns(history, width):
    if len(history) <= width:
        return [None] * (width - len(history)) + list(history)

    columns = [None] * width
    # Keep the latest sample on the right edge. Each earlier time bucket keeps its peak.
    columns[-1] = history[-1]
    if width == 1:
        return columns
    start, end = history[0][0], history[-1][0]
    step = (end - start) / (width - 1)
    first, last = 0, len(history) - 1
    for column in range(width - 1):
        if column == width - 2:
            stop = last
        elif step:
            boundary = (start + (column + 1) * step,)
            stop = bisect_left(history, boundary, first, last)
        else:
            stop = math.ceil((column + 1) * last / (width - 1))
        sample = max(history[first:stop], key=lambda point: point[1] if point[1] is not None else -1,
                     default=None)
        if sample is not None and sample[1] is not None:
            columns[column] = sample
        first = stop
    return columns


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
