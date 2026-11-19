"""SQLite storage partitioned by interface and Unix day (86,400 seconds)."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import math
import sqlite3


DAY = 86400
APPLICATION_ID = 0x4E4D4F4E  # NMON
VERSION = 1
DEFAULT_DATABASE = 'network-monitor.db'


def table_name(interface, day):
    # Lossless encoding avoids collisions and keeps interface names out of SQL.
    return 'samples_i{}_d{}'.format(interface.encode('utf-8').hex(), int(day))


class Writer:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.known = set()
        try:
            application = self.connection.execute('PRAGMA application_id').fetchone()[0]
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if application != APPLICATION_ID:
                if application or self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    raise RuntimeError('该文件不是 network-monitor 数据库')
                with self.connection:
                    self.connection.execute('BEGIN')
                    self.connection.execute('''CREATE TABLE partitions (
                        interface TEXT NOT NULL, day INTEGER NOT NULL,
                        PRIMARY KEY (interface, day)
                    ) WITHOUT ROWID''')
                    self.connection.execute('PRAGMA application_id = {}'.format(APPLICATION_ID))
                    self.connection.execute('PRAGMA user_version = {}'.format(VERSION))
            elif version != VERSION:
                raise RuntimeError('不支持的数据库版本: {}'.format(version))
            self.connection.execute('PRAGMA journal_mode = WAL')
            self.connection.execute('PRAGMA synchronous = FULL')
        except BaseException:
            self.connection.close()
            raise

    def write(self, timestamp, duration, samples):
        """Commit one sampling round; samples maps interface to (up, down)."""
        day = timestamp // DAY
        created = set()
        with self.connection:
            self.connection.execute('BEGIN')
            for interface, (up, down) in samples.items():
                table = table_name(interface, day)
                if table not in self.known:
                    self.connection.execute('''CREATE TABLE IF NOT EXISTS "{}" (
                        timestamp INTEGER NOT NULL CHECK (typeof(timestamp) = 'integer'),
                        duration_s REAL NOT NULL CHECK (duration_s > 0),
                        upload_bytes INTEGER NOT NULL CHECK (upload_bytes >= 0),
                        download_bytes INTEGER NOT NULL CHECK (download_bytes >= 0)
                    )'''.format(table))
                    self.connection.execute('CREATE INDEX IF NOT EXISTS "{0}_timestamp" ON "{0}" (timestamp)'.format(table))
                    self.connection.execute('INSERT OR IGNORE INTO partitions VALUES (?, ?)', (interface, day))
                    created.add(table)
                self.connection.execute('INSERT INTO "{}" VALUES (?, ?, ?, ?)'.format(table),
                                        (timestamp, duration, up, down))
        self.known.update(created)

    def close(self):
        self.connection.close()


@contextmanager
def reader(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError('找不到数据库: {}'.format(path))
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only = ON')
        connection.execute('BEGIN')
        if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or connection.execute('PRAGMA user_version').fetchone()[0] != VERSION):
            raise RuntimeError('该文件不是受支持的 network-monitor 数据库')
        yield connection
    finally:
        connection.close()


def tables_for(connection, interface, start, end):
    # Partition lookup uses the (interface, day) primary key, even for years of data.
    rows = connection.execute('''SELECT day FROM partitions
        WHERE interface = ? AND day >= ? AND day <= ? ORDER BY day''',
        (interface, int(start // DAY), math.ceil(end / DAY) - 1))
    return [table_name(interface, row[0]) for row in rows]


@dataclass
class Summary:
    count: int = 0
    duration: float = 0.0
    totals: list = field(default_factory=lambda: [0, 0, 0])
    minima: list = field(default_factory=lambda: [float('inf')] * 3)
    maxima: list = field(default_factory=lambda: [0.0] * 3)
    first: float = None
    last: float = None
    chart_start: float = None


def summarize(connection, tables, start, end):
    summary = Summary()
    for table in tables:
        row = connection.execute('''SELECT count(*), sum(duration_s),
            sum(upload_bytes), sum(download_bytes), min(timestamp), max(timestamp),
            min(timestamp - duration_s),
            min(upload_bytes * 8.0 / duration_s / 1e6),
            min(download_bytes * 8.0 / duration_s / 1e6),
            min((upload_bytes + download_bytes) * 8.0 / duration_s / 1e6),
            max(upload_bytes * 8.0 / duration_s / 1e6),
            max(download_bytes * 8.0 / duration_s / 1e6),
            max((upload_bytes + download_bytes) * 8.0 / duration_s / 1e6)
            FROM "{}" WHERE timestamp >= ? AND timestamp < ?'''.format(table), (start, end)).fetchone()
        if not row[0]:
            continue
        summary.count += row[0]
        summary.duration += row[1]
        for i, total in enumerate((row[2], row[3], row[2] + row[3])):
            summary.totals[i] += total
            summary.minima[i] = min(summary.minima[i], row[7 + i])
            summary.maxima[i] = max(summary.maxima[i], row[10 + i])
        summary.first = row[4] if summary.first is None else min(summary.first, row[4])
        summary.last = row[5] if summary.last is None else max(summary.last, row[5])
        summary.chart_start = row[6] if summary.chart_start is None else min(summary.chart_start, row[6])
    return summary


def chart_series(connection, tables, start, end, summary, columns):
    """Aggregate in SQL; memory stays proportional to chart width, not row count."""
    chart_start = summary.chart_start
    step = max(summary.last - chart_start, 1) / columns
    buckets = [[0.0, 0, 0] for _ in range(columns)]
    for table in tables:
        rows = connection.execute('''SELECT min(?, cast((timestamp - ?) / ? AS INTEGER)) AS bucket,
            sum(duration_s), sum(upload_bytes), sum(download_bytes)
            FROM "{}" WHERE timestamp >= ? AND timestamp < ? GROUP BY bucket'''.format(table),
            (columns - 1, chart_start, step, start, end))
        for bucket, duration, up, down in rows:
            buckets[bucket][0] += duration
            buckets[bucket][1] += up
            buckets[bucket][2] += down
    series = [[None if duration == 0 else values[i] * 8 / duration / 1e6
               for duration, *values in buckets] for i in (0, 1)]
    series.append([None if up is None else up + down for up, down in zip(*series)])
    return chart_start, summary.last, step, series
