"""Time zones and local calendar ranges, used only by statistics."""

import datetime as dt
import math
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
