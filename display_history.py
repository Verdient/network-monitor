"""History mode: statistics and trends for a stored time range."""

from itertools import chain, zip_longest

from display_base import DIRECTIONS, Canvas, clean, clip, display_width, pad, traffic


class HistoryScreens(Canvas):
    """Static reports printed once for a query range."""

    def traffic_table(self, interface, zone, unit, rows, current_period=None):
        """Per-period totals with stacked download/upload bars.

        rows holds (label, totals, period boundary) from the first period with samples.
        totals is (upload, download, total) or None when the period has no samples.
        """
        self.mode = 'history'
        title = '流量汇总 · 按' + unit
        context = ['网卡  ' + clean(interface), '时区  ' + clean(zone)]
        sampled = [i for i, (_, totals, _) in enumerate(rows) if totals is not None]
        if self.columns < 34:
            lines = [clip(title, self.columns)]
            lines.extend(clip(label + '  ' + (traffic(totals[2]) if totals else '—'), self.columns)
                         for label, totals, _ in rows)
            return '\n'.join(lines if rows else [*lines, '暂无采样数据'])
        inside = self.columns - 4
        body = [self.paint(line, 'muted') for line in self.connection_fields(context, inside)]
        if not sampled:
            body.append(self.paint('暂无采样数据', 'ink'))
            return '\n'.join(self.panel(title, body, detail=self.fitted_chip(title), detail_role=None))

        cells = [[traffic(totals[i]) for i in (1, 0, 2)] if totals else ['—'] * 3 for _, totals, _ in rows]
        sums = [sum(rows[index][1][i] for index in sampled) for i in (1, 0, 2)]
        means = [value / len(sampled) for value in sums]
        footer = [[traffic(value) for value in sums], [traffic(value) for value in means]]
        headings = ['↓ 下载', '↑ 上传', 'Σ 合计']
        label_width = max(display_width(text) for text in ['周期起点', '合计', '平均', *(row[0] for row in rows)])
        value_width = max(map(display_width, chain(headings, chain.from_iterable(cells + footer))))
        peak = max(sampled, key=lambda index: rows[index][1][2])
        scale = rows[peak][1][2]
        peak_label, ongoing_label = '▲ 峰值', '▸ 进行中'
        # Fill the width by priority: totals, period marker, bars, then the two directions.
        label_width = min(label_width, inside - 2 - value_width)
        column = 2 + value_width
        free = inside - label_width - column
        both_width = 2 + display_width(peak_label + ongoing_label)
        if rows[peak][2] == current_period and free >= both_width:
            note_width = both_width
        else:
            note_width = 1 + display_width(ongoing_label)
            if free < note_width:
                note_width = 0
        free -= note_width
        shown = [2]
        if free >= 2 * column + 12:
            shown = [0, 1, 2]
            free -= 2 * column
        bar_width = free - 2 if free >= 12 else 0

        def bar(totals):
            if not bar_width:
                return ''
            if not totals or not scale:
                return '  ' + self.paint('┄' * bar_width, 'grid')
            up, down, total = totals
            halves = max(1 if total else 0, round(total / scale * bar_width * 2))
            full, half = divmod(halves, 2)
            down_cells = min(full, round(down / scale * bar_width))
            used = full + half
            return ('  ' + self.paint('━' * down_cells, 'down') + self.paint('━' * (full - down_cells), 'up')
                    + self.paint('╸' * half, 'up') + self.paint('┄' * (bar_width - used), 'grid'))

        def row(label, values, roles, label_role='ink', strong=False):
            line = self.paint(pad(clip(label, label_width), label_width), label_role)
            for index in shown:
                line += '  ' + self.paint(pad(values[index], value_width, right=True), roles[index],
                                          bold=strong and index == 2)
            return line

        heading = self.paint(pad('周期起点', label_width) + ''.join('  ' + pad(headings[i], value_width, right=True)
                                                                 for i in shown), 'muted')
        tail_width = bar_width + 1 + note_width
        if bar_width:
            heading += '  ' + self.paint(clip('分布 · 满刻度 ' + traffic(scale) if scale else '分布', tail_width), 'muted')
        body.append(heading)
        for index, ((label, totals, boundary), values) in enumerate(zip(rows, cells)):
            ongoing = current_period == boundary
            if totals is None:
                line = row(label, values, ('muted',) * 3, label_role='muted')
            elif index == peak:
                line = row(label, values, ('down', 'up', 'total'), strong=True)
            else:
                line = row(label, values, ('down', 'up', 'ink'))
            line += bar(totals)
            if note_width:
                note = []
                if index == peak:
                    note.append(self.paint(peak_label if not ongoing or note_width >= both_width else '▲', 'total'))
                if ongoing:
                    note.append(self.paint(ongoing_label if index != peak or note_width >= both_width else '▸', 'muted'))
                if note:
                    line += ' ' + ' '.join(note)
            body.append(line)
        body.append(self.paint('─' * inside, 'grid'))
        total_line = row('合计', footer[0], ('down', 'up', 'total'), strong=True)
        if bar_width and sums[2]:
            share = sums[0] / sums[2]
            filled = round(bar_width * share)
            total_line += ('  ' + self.paint('▰' * filled, 'down') + self.paint('▱' * (bar_width - filled), 'up')
                           + ' ' + self.paint(clip('↓ {:.0%}'.format(share), note_width - 1), 'down'))
        body.append(total_line)
        if len(sampled) > 1:
            mean_line = row('平均', footer[1], ('muted',) * 3, label_role='muted')
            if bar_width:
                mean_line += '  ' + self.paint(clip('{} 个周期 · {} 个有采样'.format(len(rows), len(sampled)),
                                                    tail_width), 'muted')
            body.append(mean_line)
        return '\n'.join(self.panel(title, body, detail=self.fitted_chip(title), detail_role=None))

    def history_report(self, report, height, rows=30, rail=0):
        self.mode = 'history'
        interface = clean(report.interface)
        summary = report.summary
        live = False
        if self.columns < 34:
            header = clip('NET MONITOR · ' + interface, self.columns)
            if summary.count:
                output = [label + '  ' + traffic(summary.totals[i]) for label, i, _ in DIRECTIONS]
            else:
                output = ['暂无采样数据']
            return '\n'.join([header, *(clip(line, self.columns) for line in output)])
        lines, totals = self.overview(report, rows, live=False)
        width = self.columns - 4
        output = self.header_panel(lines)
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
