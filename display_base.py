"""Shared drawing layer for both dashboard modes: theme, frames, readouts and charts."""

from dataclasses import dataclass
from itertools import groupby
import math
import re
import unicodedata

from time_ranges import local_datetime, timezone_label


ANSI = re.compile(r'\x1b\[[0-9;]*m')
BRAILLE_DOTS = ((1, 2, 4, 64), (8, 16, 32, 128))
# The original dashboard palette. Light and portable roles pair a true-color value
# with its nearest xterm-256 color.
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
# Both modes share the original download/upload accents.
ACCENTS = {'live': 'down', 'history': 'down'}
MARK_INKS = {'live': ('down', 'up'), 'history': ('down', 'up')}
SECTION_MARK = '╺━ '
DIRECTIONS = (('↓ 下载', 1, 'down'), ('↑ 上传', 0, 'up'), ('∑ 合计', 2, 'total'))
CHANNELS = ('RX', 'TX', 'Σ')
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


class Canvas:
    """Theme-aware primitives shared by the live and history screens."""

    def __init__(self, columns, color=False, truecolor=False, indexed=False, interval=1.0,
                 theme=None, zone=None, mode='live'):
        self.columns = columns
        self.interval = interval
        self.truecolor = color and truecolor
        self.zone = zone
        self.mode = mode
        self.paging = SINGLE_PAGE
        # Columns the caller keeps blank on each side of the screen.
        self.margin = 0
        self.colors = {}
        self.title_ink = ''
        precise = self.truecolor or indexed
        if color:
            self.colors = dict(COLORS if self.truecolor else BASIC)
            if theme != 'dark':
                if precise:
                    accents = LIGHT_COLORS if theme == 'light' else PORTABLE_COLORS
                    self.colors = {role: ('1;39' if role == 'ink' else '39') for role in BASIC}
                    for role, (rgb, index) in accents.items():
                        self.colors[role] = self.palette_color(rgb, index)
                else:
                    self.colors.update(ink='1;39', frame='39', grid='39', muted='39')
                self.title_ink = self.colors['ink']
            elif self.truecolor:
                self.title_ink = '1;38;2;247;250;255'
            elif indexed:
                self.title_ink = '1;38;5;231'
            else:
                self.title_ink = '1;97'
            # The live badge reuses the download ink rather than a separate alert color.
            self.colors['alert'] = self.colors['down']
        self.wave_inks = {}
        for role, stops in WAVE_SHADES.items():
            if not color:
                inks = [''] * len(stops)
            elif not precise:
                normal, bright = ('36', '96') if role == 'down' else ('35', '95')
                if theme == 'light':
                    bright = '1;' + normal
                inks = [normal] * 4 + [bright] * 4
            elif theme != 'dark':
                waves = LIGHT_WAVES if theme == 'light' else PORTABLE_WAVES
                low, high = (self.palette_color(rgb, index) for rgb, index in waves[role])
                inks = [low] * 4 + [high] * 4
            else:
                inks = [self.palette_color(rgb, index) for rgb, index in stops]
            self.wave_inks[role] = inks

    @property
    def accent(self):
        """The chrome color that tells live and history screens apart."""
        return ACCENTS.get(self.mode, 'down')

    def palette_color(self, rgb, index):
        if self.truecolor:
            return '38;2;{};{};{}'.format(*rgb)
        return '38;5;{}'.format(index)

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

    def glitch(self, text):
        """Shift two letters into the mode's mark colors, like a chromatic glitch on a neon sign."""
        if not self.colors:
            return text
        first, second = MARK_INKS.get(self.mode, MARK_INKS['live'])
        letters = [i for i, char in enumerate(text) if char.isalpha()]
        inks = {letters[i]: self.colors[role] for i, role in ((3, first), (9, second)) if i < len(letters)}
        runs = groupby(enumerate(text), key=lambda item: inks.get(item[0], self.title_ink))
        return ''.join(self.tint(''.join(char for _, char in run), ink) for ink, run in runs)

    def panel(self, title, lines, width=None, role='ink', detail='', detail_role='muted', accented=False,
              glitch=False):
        width = self.columns if width is None else width
        frame = lambda text: self.paint(text, 'frame')
        title = ' {} '.format(clip(title, width - 8))
        room = width - 6 - display_width(title)
        trailer = ' {} '.format(detail) if detail and display_width(detail) + 3 <= room else ''
        border_width = room - display_width(trailer)
        accent = ''
        if accented and border_width >= 32:
            accent = self.paint('━╸', 'down') + self.paint('╱╱', 'up') + frame('──')
        heading = self.glitch(title) if glitch else self.tint(title, self.title_ink)
        top = (self.paint('┏━', role) + heading + self.paint('╲', role)
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

    def connection_fields(self, fields, width):
        lines = []
        line = ''
        for field in fields:
            combined = line + '  ' + field if line else field
            if line and display_width(combined) > width:
                lines.append(line)
                line = field
            else:
                line = combined
        if line:
            lines.append(line)
        return [clip(line, width) for line in lines]

    def section_heading(self, title, detail, width, role):
        detail = clean(detail)
        left = SECTION_MARK + clip(clean(title), width - display_width(SECTION_MARK))
        spare = width - display_width(left)
        if detail and display_width(detail) + 4 <= spare:
            rule = '─' * (spare - display_width(detail) - 3)
            return self.paint(left, role, bold=True) + self.paint('  ' + rule + ' ', 'frame') + self.paint(detail, 'muted')
        return self.paint(left, role, bold=True)

    def page_bar(self, title, detail, width):
        """Title row of a full-screen page: the mode tag ahead of a regular section heading."""
        chip = self.mode_chip(short=True)
        room = width - display_width(chip) - 1
        if room < 16:
            return self.section_heading(title, detail, width, 'ink')
        return chip + ' ' + self.section_heading(title, detail, room, 'ink')

    def meter(self, fraction, width, role):
        if width <= 0:
            return ''
        filled = min(width, max(1, round(fraction * width))) if fraction > 0 else 0
        return self.paint('━' * filled, role) + self.paint('┄' * (width - filled), 'grid')

    @staticmethod
    def share_label(fraction):
        return '<1%' if 0 < fraction < 0.01 else '{:.0%}'.format(fraction)

    def readout_card(self, title, value, notes, width, role, large):
        inside = width - 4
        digits = self.digits(value, role, inside) if large and self.colors else []
        if large and self.colors and not digits:
            digits = ['', self.paint(clip(value, inside), role, bold=True), '']
        lines = digits or [self.paint(clip(value, inside), role, bold=True)]
        for label, reading in notes:
            room = max(1, inside - display_width(label) - 1)
            lines.append(self.paint(label, 'muted') + ' ' + pad(reading, room, right=True))
        return self.panel(title, lines, width=width, role=role)

    def paint_button(self, text, role, enabled=True):
        if enabled:
            return self.paint(text, role, bold=True, reverse=True)
        color = self.colors.get('frame')
        return self.tint(text, '2;' + color if color else '')

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
        left, right = MARK_INKS[self.mode]
        logos = [self.paint('╶━━', left) + self.paint('╱╱', right), *mark,
                 ' ' * (mark_width - 5) + self.paint('╱╱', left) + self.paint('━━╴', right)]
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

    def overview(self, report, rows, live):
        """Return the header body and the totals the history chart still has to show."""
        interface = clean(report.interface)
        summary = report.summary
        width = self.columns - 4
        stamp = lambda value: local_datetime(value, report.zone).strftime('%Y-%m-%d %H:%M:%S %z')
        first, last = stamp(report.start), stamp(report.end)
        left, right = MARK_INKS[self.mode]
        mark = [self.paint(n, left) + ' ' + self.paint(m, right) for n, m in BLOCK_MARK]
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
        return lines, totals

    def mode_chip(self, beat=0, status=None, short=False):
        """Tag the screen as a live feed or an archived report."""
        if self.mode == 'history':
            return self.paint('◆ ARCHIVE', 'total', bold=True) + ('' if short else self.paint(' 历史统计', 'muted'))
        # The dot pulses as samples arrive.
        dot = '●' if beat % 2 == 0 else '○'
        return self.paint(dot + ' LIVE', 'alert', bold=True) + ('' if short else self.paint(' ' + (status or '实时'), 'muted'))

    def fitted_chip(self, title, beat=0, status=None, width=None):
        """The mode tag for a panel title row, shortened when the full tag would crowd the title."""
        room = (self.columns if width is None else width) - 6 - display_width(title) - 2
        chip = self.mode_chip(beat, status)
        if display_width(chip) + 3 > room:
            chip = self.mode_chip(beat, status, short=True)
        return chip

    def header_panel(self, lines, beat=0, status=None):
        title = 'NETWORK MONITOR'
        return self.panel(title, lines, role=self.accent, detail=self.fitted_chip(title, beat, status),
                          detail_role=None, accented=True, glitch=True)


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
