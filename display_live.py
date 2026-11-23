"""Live mode: an iftop-style session view backed by recent history from SQLite."""

from bisect import bisect_left
from dataclasses import dataclass
from heapq import nlargest
from ipaddress import ip_address
from itertools import zip_longest
import math
import re

from display_base import (ANSI, DIRECTIONS, SECTION_MARK, SINGLE_PAGE, Canvas, chart_scale, clean, clip,
                          display_width, line_cells, pad, place_peak_note, speed, traffic)
from time_ranges import local_datetime


IP_COLUMNS = (
    ('address', '对端 IP', 'IP'),
    ('down_mbps', '↓ 下载速率', '↓速'), ('up_mbps', '↑ 上传速率', '↑速'),
    ('download', '↓ 累计下载', '↓量'), ('upload', '↑ 累计上传', '↑量'),
)
IP_VIEWS = ('ip', 'ip-local')
# The address list keeps at least this many rows (frame, headings and a few addresses).
IP_LIST_MIN_ROWS = 8
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


def carrier_label(name):
    text = re.sub(r'[\s_]+', ' ', name)
    return next((label for pattern, label in CARRIER_ALIASES if pattern.search(text)), name)


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


def address_pages(heights, room, shortcuts=False):
    """Pack complete address rows, including IPv6 and namespace continuations."""
    pages = []
    start = used = end = 0
    for index, height in enumerate(heights):
        if used and (used + height > room or (shortcuts and index - start == 9)):
            pages.append((start, index))
            start, used = index, 0
        used += height
        end = index + 1
    return [*pages, (start, end)]


class LiveScreens(Canvas):
    """Traffic home, TCP connections and per-IP traffic for an interactive session."""

    def live_home(self, report, height, live_sample, rows=30, rail=0, connections=None, click_targets=None,
                  *, ip_capture=None, ip_page=0, ip_sort=None):
        self.mode = 'live'
        interface = clean(report.interface)
        if self.columns < 34 or rows < 17:
            header = clip('NET MONITOR · ' + interface, self.columns)
            measurements = self.live_readings(live_sample, self.columns)
            lines = [header, *measurements]
            if connections and self.columns >= 24 and rows - len(lines) >= 4:
                sockets, _ = self.connection_panel(connections, self.columns, rows - len(lines) - 1)
                lines.extend(sockets)
            elif connections and len(lines) + 2 <= rows:
                snap = connections[0]
                label = 'TCP 部分' if snap.partial else '本机 TCP'
                lines.append(clip(label + '  ' + (snap.status or '已建立 {}'.format(snap.established)), self.columns))
            return self.live_screen(lines, rows, click_targets=click_targets)
        lines, _ = self.overview(report, rows, live=True)
        rates_in_cards = self.columns >= 48
        status = live_sample.status if rates_in_cards and live_sample.rates is None else None
        if not rates_in_cards:
            lines.extend(self.live_readings(live_sample, self.columns - 4))
        count = report.summary.count
        # Each sample advances the pulse; fast refreshes still blink about once per second.
        beat = count if self.interval >= 1 else int(count * self.interval)
        output = self.header_panel(lines, beat, status)
        space = rows - len(output) - 1
        rates = (live_sample.rates or (None, None, None)) if rates_in_cards else None
        if (ip_capture is None or not ip_capture.ready or ip_capture.status
                or not ip_preview_fits(self.columns, rows, rail)):
            ip_capture = None
        body, paging = self.live_body(report, rates, height, rail, space, connections, ip_capture=ip_capture,
                                      ip_page=ip_page, ip_sort=ip_sort, click_targets=click_targets,
                                      row_offset=len(output))
        return self.live_screen(output + body, rows, paging=paging, click_targets=click_targets)

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
        title = 'TCP 连接 · 部分' if snapshot.partial else 'TCP 连接 · 本机'
        return self.panel(title, lines[:budget - 2], width=width, role='ink'), paging

    def port_summary(self, group, width, cards, large):
        usage = group.ports
        fraction = usage.fraction if usage else None
        ratio = '{:.1%}'.format(fraction) if fraction is not None else '—'
        occupied = '{:,}/{:,}'.format(usage.used, usage.total) if usage else '—'
        remaining = '{:,}'.format(usage.remaining) if usage else '—'
        capacity = '{:,}'.format(group.new_target_remaining) if group.new_target_remaining is not None else '—'
        if not cards:
            occupancy = ['临时端口容量未知'] if usage is None else ['端口占用 ' + occupied + ' ' + ratio,
                                                               '独占估计剩余 ' + remaining, '新目标容量 ' + capacity]
            direction = ['出站已建立 {:,}'.format(group.outgoing), '入站已建立 {:,}'.format(group.incoming),
                         '入站对端 {:,}'.format(group.incoming_peers)]
            return ([self.paint(line, 'total') for line in self.connection_fields(occupancy, width)]
                    + [self.paint(line, 'ink') for line in self.connection_fields(direction, width)])

        left = (width - 1) // 2
        right = width - left - 1
        role = 'up' if fraction is not None and fraction >= .8 else 'total'
        meter_width = min(12, max(0, left - display_width(occupied + ratio) - 15))
        pressure = self.meter(fraction, meter_width, role) + ' ' if fraction is not None and meter_width else ''
        allocation = [('占用', pressure + self.paint(occupied + ' ' + ratio, role)),
                      ('新目标容量', self.paint(capacity, 'ink'))]
        directions = [('入站已建立', self.paint('{:,}'.format(group.incoming), 'down')),
                      ('入站对端', self.paint('{:,}'.format(group.incoming_peers), 'ink'))]
        port_card = self.readout_card('独占估计剩余' if usage else '临时端口容量未知', remaining + ' 个',
                                      allocation, left, role, large)
        out_card = self.readout_card('出站已建立', '{:,} 条'.format(group.outgoing), directions, right, 'up', large)
        return [a + ' ' + b for a, b in zip(port_card, out_card)]

    def connection_details_panel(self, snapshot, history, width, budget, page, group=None):
        title = 'TCP 连接 · ' + clean(snapshot.interface)
        lines = [self.page_bar(title, '部分连接表可读' if snapshot.partial else '本机 TCP', width)]
        if budget >= 12:
            counts = ['已建立 {:,}'.format(snapshot.established), '监听 {:,}'.format(snapshot.listening),
                      'TIME_WAIT {:,}'.format(snapshot.time_wait), '其他 {:,}'.format(snapshot.other)]
            lines.extend(self.paint(line, 'muted') for line in self.connection_fields(counts, width))
        chart_room = 6 if history and budget >= 22 and width >= 42 else 0
        if snapshot.address_status:
            lines.append(self.paint(clip(snapshot.address_status, width), 'muted'))
        if not snapshot.local:
            return lines + [self.paint(clip('暂无可统计的本地 IP', width), 'muted')], SINGLE_PAGE

        room = budget - len(lines) - chart_room
        cards = width >= 76 and room >= 13
        large = room >= 18
        inside = width - 4
        table = inside >= 70
        meter_width = 8 if inside >= 100 else 0
        est_width = 10 if inside >= 88 else 0
        capacity_width, free_width, wait_width = 22 + meter_width, 11, 11
        addr_width = inside - capacity_width - free_width - wait_width - est_width

        def endpoint(target):
            addr, port = target.endpoint
            return '[{}]:{}'.format(addr, port) if ':' in addr else '{}:{}'.format(addr, port)

        # Size each page from terminal geometry. Capacity also reserves continuation
        # lines for full IPv6 endpoints, independently of the values being refreshed.
        group = group or snapshot.local[0]
        context = []
        if group.namespace:
            context.append('命名空间 {}'.format(group.namespace[1]))
        if group.unknown:
            context.append('方向未知 {:,}'.format(group.unknown))
        detail = '  ·  '.join(context)
        address = group.address
        first = max(1, width - display_width(SECTION_MARK + '本地 '))
        head = self.section_heading('本地 ' + address[:first], detail, width, 'down')
        identity = [head, *(self.paint(address[i:i + width], 'down')
                            for i in range(first, len(address), width))]
        if detail and detail not in head:
            identity.extend(self.paint(line, 'muted') for line in self.connection_fields(context, width))
        header = identity + self.port_summary(group, width, cards, large)
        longest = max((len(t.endpoint[0]) + len(str(t.endpoint[1])) + (3 if ':' in t.endpoint[0] else 1)
                       for t in group.targets), default=1)
        address_rows = math.ceil(longest / (addr_width if table else inside))
        row_height = address_rows if table else address_rows + (1 if inside >= 46 else 2)
        data_room = room - len(header) - 2 - int(table)
        split = bool(group.targets) and data_room < row_height
        if split:
            data_room = room - len(identity) - 2 - int(table)
        capacity = max(1, data_room // row_height)
        page_count = int(split) + max(1, math.ceil(len(group.targets) / capacity))
        page = min(max(page, 0), page_count - 1)
        index = page
        if split:
            if index == 0:
                return (lines + header)[:budget], (page, page_count)
            header = identity
            index -= 1
        lines.extend(header)
        if len(lines) + 3 > budget:
            return lines[:budget], (page, page_count)

        offset = index * capacity
        targets = group.targets[offset:offset + capacity]
        detail = '{}–{} / {} 个目标'.format(offset + 1, offset + len(targets), len(group.targets)) if targets else ''
        body = []
        if table and targets:
            columns = [('目标地址', addr_width), ('已建立', est_width), ('端口占用 / 利用率', capacity_width),
                       ('估计剩余', free_width), ('TIME_WAIT', wait_width)]
            body.append(self.paint(''.join(pad(name, size, right=i > 0) for i, (name, size) in enumerate(columns)
                                           if size), 'muted'))
        for target in targets:
            name = endpoint(target)
            usage = target.usage
            fraction = usage.fraction if usage else None
            ratio = '{:.1%}'.format(fraction) if fraction is not None else '—'
            occupancy = '{:,}/{:,} {}'.format(usage.used, usage.total, ratio) if usage else '容量未知'
            free = '{:,}'.format(usage.remaining) if usage else '—'
            wait = '{:,}'.format(target.time_wait)
            role = 'up' if fraction is not None and fraction >= .8 else 'total'
            if table:
                prefix = self.meter(fraction, meter_width - 1, role) + ' ' if meter_width and fraction is not None else ''
                row = self.paint(pad(name[:addr_width], addr_width), 'ink')
                if est_width:
                    row += self.paint(pad('{:,}'.format(target.established), est_width, True), 'ink')
                body.append(row + pad(prefix + self.paint(occupancy, role), capacity_width, True)
                            + self.paint(pad(free, free_width, True), role, bold=True)
                            + self.paint(pad(wait, wait_width, True), 'muted'))
                body.extend(self.paint(name[i:i + addr_width], 'ink') for i in range(addr_width, len(name), addr_width))
            else:
                body.extend(self.paint(name[i:i + inside], 'ink') for i in range(0, len(name), inside))
                fields = [occupancy, '估计剩余 ' + free, 'TIME_WAIT ' + wait]
                body.extend(self.paint(line, role) for line in self.connection_fields(fields, inside))
        if not targets:
            body.append(self.paint('暂无可识别的出站目标', 'muted'))
        available = budget - len(lines) - chart_room - 2
        lines.extend(self.panel('出站目标', body[:available], width=width, detail=detail, accented=True))
        if chart_room:
            trace_rows = min(15, budget - len(lines) - 2)
            trend = self.connection_trend(history, inside, max(4, trace_rows))
            detail = ANSI.sub('', trend[0]).removeprefix('已建立趋势').strip(' ·')
            lines.extend(self.panel('已建立趋势', trend[1:], width=width, detail=detail))
        return lines, (page, page_count)

    def live_footer(self, row, view, paging, click_targets):
        page, page_count = paging
        period = '{:g}s'.format(self.interval)
        tcp_view = view in ('connections', 'connection-detail')
        navigation = [('q', '退出', 'total'), ('c', '返回' if tcp_view else '连接', 'up'),
                      ('i', '返回' if view in IP_VIEWS else 'IP', 'down')]
        if view in IP_VIEWS:
            navigation.append(('m', '对端 IP' if view == 'ip-local' else '本机 IP', 'ink'))
        if view == 'connection-detail':
            navigation.append(('b', '总览', 'down'))
        pages = [('p', '上页', 'ink'), ('n', '下页', 'ink')] if tcp_view and page_count > 1 else []
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

    def connection_overview(self, snapshot, history, rows, page, click_targets):
        width = self.columns
        inside = width - 4
        groups = sorted(snapshot.local, key=lambda g: (g.namespace is not None, g.namespace or (),
                                                       ip_address(g.address).version, int(ip_address(g.address))))
        title = 'TCP 连接 · ' + clean(snapshot.interface)
        detail = '{} 个地址{}'.format(len(groups), ' · 部分' if snapshot.partial else '')
        lines = [self.page_bar(title, detail, width)]
        if rows >= 18 and width >= 60:
            states = ['本机已建立 {:,}'.format(snapshot.established), '监听 {:,}'.format(snapshot.listening),
                      'TIME_WAIT {:,}'.format(snapshot.time_wait), '其他 {:,}'.format(snapshot.other)]
            lines.extend(self.paint(line, 'muted') for line in self.connection_fields(states, width))
        if snapshot.address_status:
            lines.append(self.paint(clip(snapshot.address_status, width), 'muted'))

        columns = [('出站', 8, 'up'), ('入站', 8, 'down'), ('入站对端', 9, 'ink'),
                   ('端口占用', 10, 'total'), ('独占剩余', 10, 'total'), ('目标最少剩余', 14, 'total')]
        if inside < 90:
            columns.pop(2)
        if inside < 66:
            columns.pop()
        if inside < 50:
            columns = [('出站', 6, 'up'), ('独占剩余', 9, 'total')]
        if inside < 30:
            columns = [('独占剩余', 9, 'total')]
        address_width = inside - sum(size for _, size, _ in columns) - 3

        def address_lines(group):
            label = clean(group.address)
            result = [label[i:i + address_width] for i in range(0, len(label), address_width)]
            if group.namespace is not None:
                result.append('命名空间 {}'.format(group.namespace[1]))
            return result

        room = max(1, rows - 1 - len(lines) - 4)
        pages = address_pages((len(address_lines(g)) for g in groups), room, shortcuts=True)
        page = min(max(0, page), len(pages) - 1)
        start, stop = pages[page]
        body = [self.paint('   ' + pad(clip('本地 IP', address_width), address_width) + ''.join(
            pad(label, size, True) for label, size, _ in columns), 'muted')]
        for number, group in enumerate(groups[start:stop], 1):
            address = address_lines(group)
            row = self.paint(' {} '.format(number), 'down') + self.paint(pad(address[0], address_width), 'ink')
            usage = group.ports
            fraction = usage.fraction if usage else None
            # Targets are ranked by occupied ports within this address's port pool.
            tightest = group.targets[0].usage if group.targets else None
            values = {'出站': group.outgoing, '入站': group.incoming, '入站对端': group.incoming_peers,
                      '端口占用': '{:.1%}'.format(fraction) if fraction is not None else None,
                      '独占剩余': usage.remaining if usage else None, '目标最少剩余': tightest.remaining if tightest else None}
            for label, size, role in columns:
                value = values[label]
                reading = format(value, ',') if isinstance(value, int) else value or '—'
                row += self.paint(pad(clip(reading, size - 1), size, True), role if value is not None else 'muted')
            action = ('tcp-address', group.namespace, group.address)
            for line in [row, *('   ' + self.paint(clip(part, inside - 3), 'muted') for part in address[1:])]:
                screen_row = len(lines) + len(body) + 1
                if click_targets is not None and screen_row < rows - 2:
                    click_targets.append((screen_row, 1, width - 1, action))
                body.append(line)
        keys = '1' if stop - start == 1 else '1–{}'.format(stop - start)
        hint = '按 {} 或点击地址查看明细'.format(keys)
        lines.extend(self.panel('本地地址总览', body[:room + 1], detail='{}/{}'.format(page + 1, len(pages))))
        lines.append(self.paint(clip(hint, width), 'muted'))
        spare = rows - len(lines) - 1
        if history and spare >= 6 and width >= 42:
            trend = self.connection_trend(history, inside, min(12, spare - 2))
            detail = ANSI.sub('', trend[0]).removeprefix('已建立趋势').strip(' ·')
            lines.extend(self.panel('已建立趋势', trend[1:], detail=detail))
        return lines, (page, len(pages))

    def connection_screen(self, connections, rows, page, click_targets=None, *, selected=None):
        self.mode = 'live'
        snap, history = connections
        if self.columns < 24 or rows < 4:
            label = 'TCP 部分已建立' if snap.partial else 'TCP 已建立'
            lines = [clip(snap.status or '{} {}'.format(label, snap.established), self.columns)]
            paging = SINGLE_PAGE
        elif selected is not None:
            group = next((g for g in snap.local if (g.namespace, g.address) == selected), None)
            if group is None:
                lines, paging = [clip(snap.status or '地址已移除，按 b 返回总览', self.columns)], SINGLE_PAGE
            else:
                lines, paging = self.connection_details_panel(snap, history, self.columns, rows - 1, page, group)
        elif len(snap.local) > 1:
            lines, paging = self.connection_overview(snap, history, rows, page, click_targets)
        elif snap.interface is not None:
            lines, paging = self.connection_details_panel(*connections, self.columns, rows - 1, page)
        else:
            lines, paging = self.connection_panel(connections, self.columns, rows - 1, page)
        return self.live_screen(lines, rows, 'connection-detail' if selected else 'connections', paging, click_targets)

    def ip_screen(self, capture, rows, page, sort=None, click_targets=None, *, view='ip'):
        self.mode = 'live'
        local = view == 'ip-local'
        peers = capture.local_rows if local else capture.rows
        width = self.columns
        header = []
        down = sum(peer.down_mbps for peer in peers)
        up = sum(peer.up_mbps for peer in peers)
        if width >= 36 and rows >= 11:
            title = ('本机 IP' if local else '对端 IP') + ' · ' + clean(capture.interface)
            active = sum(peer.down_mbps + peer.up_mbps > 0 for peer in peers)
            header.append(self.page_bar(title, '{} 个地址  ·  {} 活跃'.format(len(peers), active), width))
            if peers:
                if local and len(peers) > 1:
                    totals = ['地址合计', '↓ ' + speed(down), '↑ ' + speed(up)]
                    header.extend(self.paint(line, 'ink') for line in self.connection_fields(totals, width))
                elif width >= 60 and rows >= 16:
                    cards = []
                    directions = (('↓ 下载速率', down, sum(peer.download for peer in peers), 'down'),
                                  ('↑ 上传速率', up, sum(peer.upload for peer in peers), 'up'))
                    for index, (label, rate, size, role) in enumerate(directions):
                        card_width = (width - 1 + index) // 2
                        share = rate / (down + up) if down + up else 0
                        bar = self.meter(share, min(16, card_width - 18), role)
                        mix = self.share_label(share) if down + up else '—'
                        notes = [('累计', self.paint(traffic(size), 'ink')),
                                 ('速率占比', bar + ' ' + self.paint(mix, role))]
                        cards.append(self.readout_card(label, speed(rate), notes, card_width, role, rows >= 23))
                    header.extend(a + ' ' + b for a, b in zip(*cards))
                else:
                    header.extend(self.connection_fields(['↓ ' + speed(down), '↑ ' + speed(up)], width))
        controls = [] if click_targets is not None else None
        room = rows - 1 - len(header)
        # Reserve the comparison before the list so a long list pages instead of crowding it out.
        rates = []
        if width >= 64 and room - IP_LIST_MIN_ROWS >= 6:
            rates = self.address_rates(peers, width, min(11, room - IP_LIST_MIN_ROWS), local=local)
        lines, paging = self.ip_panel(capture, width, room - len(rates), page, sort, controls,
                                      view=view, summary=bool(header), combined_rate=down + up)
        if click_targets is not None:
            click_targets.extend((row + len(header), left, right, key) for row, left, right, key in controls)
        body = header + lines + rates
        return self.live_screen(body, rows, view, paging, click_targets)

    def address_rates(self, peers, width, budget, *, local=False):
        if local:
            if len(peers) > budget - 3 or not any(p.down_mbps or p.up_mbps for p in peers):
                return []
            top = sorted(peers, key=lambda p: (ip_address(p.address).version, int(ip_address(p.address))))
        else:
            active = (peer for peer in peers if peer.down_mbps + peer.up_mbps > 0)
            top = nlargest(min(8, budget - 3), active, key=lambda peer: peer.down_mbps + peer.up_mbps)
        if len(top) < 2:
            return []
        inside = width - 4
        address_width = min(max(len(peer.address) for peer in top), inside - 34)
        half = (inside - address_width - 4) // 2
        peak = max(max(peer.down_mbps, peer.up_mbps) for peer in top)
        left = self.paint(pad('下载 ←', half, True), 'down')
        right = self.paint('→ 上传', 'up')
        lines = [pad('地址', address_width) + '  ' + left + ' │' + right]
        for peer in top:
            down = max(1, round(half * peer.down_mbps / peak)) if peer.down_mbps else 0
            up = max(1, round(half * peer.up_mbps / peak)) if peer.up_mbps else 0
            bars = (self.paint(pad('━' * down, half, True), 'down') + self.paint(' │', 'grid')
                    + self.paint('━' * up, 'up'))
            lines.append(self.paint(pad(clip(peer.address, address_width), address_width), 'muted') + '  ' + bars)
        return self.panel('双向速率对比', lines, width=width, detail='满刻度 ' + speed(peak))

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

    def ip_panel(self, capture, columns, budget, page, sort=None, click_targets=None, *, view='traffic',
                 summary=False, combined_rate=0):
        width = columns - 4
        local = view == 'ip-local'
        address_label = '本机 IP' if local else '对端 IP'
        title = (address_label if view in IP_VIEWS else 'IP 流量') + ' · ' + clean(capture.interface)
        if summary:
            title = '本机地址流量' if local else '对端流量'
        peers = capture.local_rows if local else capture.rows
        status = clean(capture.status)
        if not status and not peers:
            status = '网卡暂无可统计的 IP' if local and capture.ready else '等待 IP 流量'
        if columns < 18 or budget < 4:
            return [clip(title, columns), clip(status or '{} {}'.format(address_label, len(peers)), columns)], SINGLE_PAGE

        if local and sort is None:
            sort = ('address', False)
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
        compact_columns = local and 51 <= width < 69
        wide = compact_columns or (width >= 69 if summary else width >= max(67, longest + 52))
        share_width = 15 if summary and width >= 91 and (not local or width >= longest + 67) else 0
        metric_widths = (11, 11, 9, 9) if compact_columns else (13, 13, 13, 13)
        addr_width = width - sum(metric_widths) - share_width if wide else width
        details_for = None if local else getattr(capture, 'peer_details', None)
        # A fixed second row gives the labels room without repaging when lookups complete.
        separate_details = details_for is not None and longest + 50 > addr_width
        # The compact IP header needs four cells and the other headers need five.
        per_row = len(IP_COLUMNS) if wide else (width + 1) // 5
        lines = []
        for group in range(0, len(IP_COLUMNS), per_row):
            left = 2  # The panel border and its inside padding precede the header.
            cells = []
            for index, (column, label, short) in enumerate(IP_COLUMNS[group:group + per_row], group):
                if local and column == 'address':
                    label = address_label
                selected = sort is not None and sort[0] == column
                indicator = ('▾' if sort[1] else '▴') if selected else ' '
                cell_width = (addr_width if column == 'address' else metric_widths[index - 1]) if wide else display_width(short) + 2
                text = '{} {}'.format(label if wide and not compact_columns else short, indicator)
                cells.append(self.paint(pad(text, cell_width, right=wide and column != 'address'),
                                        'ink' if selected else 'muted'))
                if click_targets is not None:
                    click_targets.append((len(lines) + 1, left, left + cell_width, column))
                left += cell_width
            if share_width:
                cells.append(self.paint(pad('速率占比', share_width, True), 'muted'))
            lines.append(''.join(cells))
        if status and len(lines) < budget - 2:
            lines.append(self.paint(clip(status, width), 'muted'))
        room = budget - 2 - len(lines)
        extra = (0 if wide else 2) + separate_details
        if local:
            ranges = address_pages((math.ceil(len(peer.address) / addr_width) + extra for peer in peers), room)
            pages = len(ranges)
            page = min(max(page, 0), pages - 1)
            offset, stop = ranges[page]
        else:
            peer_rows = max(1, math.ceil(longest / addr_width)) + extra
            capacity = max(1, room // peer_rows)
            pages = max(1, math.ceil(len(peers) / capacity))
            page = min(max(page, 0), pages - 1)
            offset = page * capacity
            stop = min(offset + capacity, len(peers))
        hostname_for = None if local else getattr(capture, 'hostname', None)
        for peer in peers[offset:stop]:
            hostname = hostname_for(peer.address) if hostname_for is not None else ''
            location, carrier, asn = details_for(peer.address) if details_for is not None else ('', '', '')
            location = re.sub(r'州(?=·美国$)', '', location)
            fields = (location, carrier_label(carrier), asn, hostname)
            inline_fields = () if separate_details else fields
            values = (speed(peer.down_mbps), speed(peer.up_mbps), traffic(peer.download), traffic(peer.upload))
            if wide:
                line = pad(self.ip_address_line(peer.address[:addr_width], inline_fields, addr_width), addr_width)
                for i, (value, role) in enumerate(zip(values, ('down', 'up', 'down', 'up'))):
                    if summary:
                        if i >= 2:
                            role = 'peer_location'
                        elif (peer.down_mbps, peer.up_mbps)[i] == 0:
                            role = 'muted'
                    size = metric_widths[i]
                    reading = value.replace(' ', '') if compact_columns else value
                    line += self.paint(pad(clip(reading, size - 1), size, True), role,
                                       bold=summary and i < 2 and role != 'muted')
                if share_width:
                    share = (peer.down_mbps + peer.up_mbps) / combined_rate if combined_rate else 0
                    percent = self.share_label(share) if combined_rate else '—'
                    line += pad(self.meter(share, 7, 'total') + ' ' + pad(percent, 4, True), share_width, True)
                lines.append(line)
                lines.extend(self.paint(peer.address[i:i + addr_width], 'ink')
                             for i in range(addr_width, len(peer.address), addr_width))
            else:
                address = [peer.address[i:i + width] for i in range(0, len(peer.address), width)]
                lines.extend(self.paint(part, 'ink') for part in address[:-1])
                lines.append(self.ip_address_line(address[-1], inline_fields, width))
            if separate_details:
                indent = self.paint('  ╰ ', 'frame') if summary and any(fields) else ''
                lines.append(indent + self.ip_address_line('', fields, width - display_width(indent)))
            if not wide:
                if summary and width >= 38:
                    half = width // 2
                    lines.append(pad(self.paint('↓ ' + values[0], 'down'), half) + self.paint('↑ ' + values[1], 'up'))
                    lines.append(self.paint(pad('累计 ' + values[2], half) + '累计 ' + values[3], 'muted'))
                else:
                    for arrow, role, rate, total in (('↓', 'down', values[0], values[2]), ('↑', 'up', values[1], values[3])):
                        lines.append(self.paint(clip('{} {}  ·  累计 {}'.format(arrow, rate, total), width), role))
        lines = lines[:budget - 2]
        if not summary or pages > 1:
            lines.extend([''] * (budget - 2 - len(lines)))
        detail = '{}–{} / {}'.format(offset + 1, stop, len(peers)) if summary and peers else ''
        panel = self.panel(title, lines, width=columns, detail=detail, accented=summary)
        if pages > 1:
            panel[-1] = self.ip_page_controls(columns, budget - 1, page, pages, click_targets)
        return panel, (page, pages)


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
