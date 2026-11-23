"""Render terminal dashboards without terminal or database dependencies.

The software has two modes. Live screens (display_live) follow an interactive
session, like iftop with recent history added; history screens
(display_history) print statistics for a stored range. Both share one canvas
(display_base) so their themes, frames and charts stay consistent.
"""

from display_base import (ANSI, DIRECTIONS, SINGLE_PAGE, Canvas, Report, area_cells, chart_scale, clean, clip,
                          display_width, elapsed, line_cells, pad, speed, time_labels, traffic)
from display_history import HistoryScreens
from display_live import (IP_COLUMNS, IP_VIEWS, LiveSample, LiveScreens, address_pages, carrier_label,
                          connection_columns, ip_preview_budget, ip_preview_fits, metric_rail_width)

__all__ = [
    'ANSI', 'DIRECTIONS', 'IP_COLUMNS', 'IP_VIEWS', 'SINGLE_PAGE', 'Canvas', 'Dashboard', 'LiveSample', 'Report',
    'address_pages', 'area_cells', 'carrier_label', 'chart_scale', 'clean', 'clip', 'connection_columns',
    'display_width', 'elapsed', 'ip_preview_budget', 'ip_preview_fits', 'line_cells', 'metric_rail_width', 'pad',
    'speed', 'time_labels', 'traffic',
]


class Dashboard(LiveScreens, HistoryScreens):
    def render(self, report, height, live_sample=None, rows=30, rail=0, connections=None, click_targets=None,
               *, ip_capture=None, ip_page=0, ip_sort=None):
        if live_sample is None:
            return self.history_report(report, height, rows=rows, rail=rail)
        return self.live_home(report, height, live_sample, rows=rows, rail=rail, connections=connections,
                              click_targets=click_targets, ip_capture=ip_capture, ip_page=ip_page, ip_sort=ip_sort)
