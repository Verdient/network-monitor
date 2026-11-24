"""Time zones and local calendar ranges, used only by statistics."""

import datetime as dt
import math
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def timezone(value):
    if value is None or value.lower() in ('local', 'system'):
        # A fixed offset from datetime.now().astimezone() would break historical DST.
        return None
    if value.upper() in ('UTC', 'Z'):
        return dt.timezone.utc
    match = re.fullmatch(r'(?:UTC)?([+-])(\d{2}):(\d{2})', value, re.IGNORECASE)
    if match:
        sign, hours, minutes = match.groups()
        if int(hours) > 23 or int(minutes) > 59:
            raise ValueError('无效的 UTC 时差: ' + value)
        offset = dt.timedelta(hours=int(hours), minutes=int(minutes))
        return dt.timezone(offset if sign == '+' else -offset)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('未知时区: {}；例如 Asia/Shanghai、UTC 或 +08:00'.format(value)) from None


def local_datetime(timestamp, zone):
    value = dt.datetime.fromtimestamp(timestamp, zone)
    return value.astimezone() if zone is None else value


def zone_name_from_path(path):
    try:
        parts = Path(path).resolve().parts
        start = parts.index('zoneinfo') + 1
    except (OSError, RuntimeError, ValueError):
        return ''
    if parts[start:start + 1] in (('posix',), ('right',)):
        start += 1
    return '/'.join(parts[start:])


def timezone_label(zone):
    if zone is not None:
        return str(zone)

    setting = os.environ.get('TZ')
    if setting is None:
        name = zone_name_from_path('/etc/localtime')
        if not name:
            try:
                name = Path('/etc/timezone').read_text(encoding='utf-8').strip()
            except (OSError, UnicodeError):
                name = ''
    else:
        name = setting.removeprefix(':')
        if name.startswith('/'):
            name = zone_name_from_path(name)

    if name:
        try:
            # Ignore POSIX TZ strings and files whose identifiers are unavailable.
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            name = ''
    if not name:
        local = dt.datetime.now().astimezone()
        name = local.tzname() or str(local.tzinfo)
    return '{}（系统）'.format(name)


def parse_time(value, zone):
    try:
        timestamp = float(value)
    except ValueError:
        try:
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('时间应为 Unix 秒、YYYY-MM-DD 或 ISO 日期时间') from None
        if value.tzinfo is not None:
            return value.timestamp()
        timestamp = value.replace(tzinfo=zone).timestamp()
        if local_datetime(timestamp, zone).replace(tzinfo=None) != value:
            raise ValueError('该本地时间不存在（夏令时切换），请指定带 UTC 时差的时间')
        return timestamp
    if not math.isfinite(timestamp):
        raise ValueError('Unix 时间戳必须为有限数值')
    # Reject unrenderable numeric timestamps before querying the database.
    local_datetime(timestamp, zone)
    return timestamp


def resolve_range(start=None, end=None, zone=None, now=None):
    today = local_datetime(time.time() if now is None else now, zone).date()
    tomorrow = today + dt.timedelta(days=1)
    left = parse_time(start, zone) if start is not None else dt.datetime.combine(today, dt.time(), zone).timestamp()
    right = parse_time(end, zone) if end is not None else dt.datetime.combine(tomorrow, dt.time(), zone).timestamp()
    if left >= right:
        raise ValueError('开始时间必须早于结束时间；查询范围为 [start, end)')
    return left, right


def day_range(value, zone):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('--day 需要 YYYY-MM-DD 格式的日期')
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError('--day 日期无效: ' + value) from None
    start = dt.datetime.combine(day, dt.time(), zone).timestamp()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), zone).timestamp()
    if start >= end:
        raise ValueError('该日期在所选时区不存在: ' + value)
    return start, end


def is_day_range(start, end, zone):
    start_day = local_datetime(start, zone).date()
    end_day = local_datetime(end, zone).date()
    return end_day > start_day and (start, end) == resolve_range(zone=zone, now=start)


def period_start(timestamp, unit, zone):
    local = local_datetime(timestamp, zone)
    if unit in ('day', 'month'):
        day = local.date() if unit == 'day' else local.date().replace(day=1)
        return dt.datetime.combine(day, dt.time(), zone).timestamp()
    if unit == 'second':
        return timestamp
    start = timestamp - local.second
    if unit == 'hour':
        start -= local.minute * 60
    # A clock change can start a partial hour or minute. Keep repeated periods
    # distinct and use the transition itself as the boundary of a partial period.
    if local_datetime(start, zone).utcoffset() != local.utcoffset():
        before, after = start, timestamp
        while before + 1 < after:
            middle = (before + after) // 2
            if local_datetime(middle, zone).utcoffset() == local.utcoffset():
                after = middle
            else:
                before = middle
        start = after
    return start


def traffic_periods(unit, count, start, end, zone):
    periods = []
    for _ in range(count):
        if start is not None and end <= start:
            break
        # Stored timestamps are integer seconds and the right edge is exclusive.
        boundary = period_start(math.ceil(end) - 1, unit, zone)
        left = boundary if start is None else max(start, boundary)
        periods.append((boundary, left, end))
        end = left
    return periods[::-1]
