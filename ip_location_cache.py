"""Store per-user lookup results and coordinate API quotas across sessions."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import time


DAY = 24 * 60 * 60
LOCATION_FORMAT_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    address TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    expires REAL NOT NULL,
    carrier TEXT,
    city_checked INTEGER NOT NULL DEFAULT 0,
    asn TEXT
);
CREATE INDEX IF NOT EXISTS locations_by_expiry ON locations (expires);
CREATE TABLE IF NOT EXISTS requests (
    provider TEXT NOT NULL,
    sent REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_by_provider ON requests (provider, sent);
CREATE TABLE IF NOT EXISTS cooldowns (
    provider TEXT PRIMARY KEY,
    until REAL NOT NULL
);
"""


def cache_path():
    home = Path.home()
    if sys.platform == 'darwin':
        base = home / 'Library/Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME') or home / '.cache').expanduser()
    return base / 'network-monitor/ip-location.sqlite3'


def open_cache(path):
    path = path or cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    database = sqlite3.connect(path, timeout=2)
    try:
        database.executescript(SCHEMA)
        with database:
            database.execute('BEGIN IMMEDIATE')
            columns = {column[1] for column in database.execute('PRAGMA table_info(locations)')}
            if 'carrier' not in columns:
                database.execute('ALTER TABLE locations ADD COLUMN carrier TEXT')
            if 'city_checked' not in columns:
                database.execute('ALTER TABLE locations ADD COLUMN city_checked INTEGER NOT NULL DEFAULT 0')
            if 'asn' not in columns:
                database.execute('ALTER TABLE locations ADD COLUMN asn TEXT')
            now = time.time()
            database.execute('DELETE FROM locations WHERE expires <= ?', (now,))
            database.execute('DELETE FROM requests WHERE sent <= ?', (now - DAY,))
            database.execute('DELETE FROM cooldowns WHERE until <= ?', (now,))
    except sqlite3.Error:
        database.close()
        raise
    return closing(database)


def cached_location(database, address, now):
    return database.execute(
        'SELECT label, carrier, expires, city_checked, asn FROM locations WHERE address = ? AND expires > ?',
        (address, now),
    ).fetchone()


def save_location(database, address, label, carrier, asn, expires, now):
    with database:
        database.execute('DELETE FROM locations WHERE expires <= ?', (now,))
        # Keep useful values from a concurrent lookup when a source returns partial data.
        database.execute("""
            INSERT INTO locations (address, label, carrier, expires, city_checked, asn) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (address) DO UPDATE SET
                label = CASE
                    WHEN excluded.label = '' THEN locations.label
                    WHEN substr(locations.label, -length(excluded.label) - 1) = '·' || excluded.label
                        THEN locations.label
                    ELSE excluded.label
                END,
                carrier = COALESCE(NULLIF(excluded.carrier, ''), locations.carrier, ''),
                expires = excluded.expires,
                city_checked = excluded.city_checked,
                asn = COALESCE(NULLIF(excluded.asn, ''), locations.asn, '')
            WHERE excluded.label <> '' OR excluded.carrier <> '' OR excluded.asn <> ''
                OR (locations.label = '' AND COALESCE(locations.carrier, '') = '' AND COALESCE(locations.asn, '') = '')
            """, (address, label, carrier, expires, LOCATION_FORMAT_VERSION, asn))


def reserve_request(database, provider, now):
    """Record a request or return its next eligible time and whether to wait for pacing."""
    with database:
        # Start the write transaction before checking the cap shared with other processes.
        database.execute('DELETE FROM requests WHERE provider = ? AND sent <= ?', (provider.name, now - DAY))
        cooldown = database.execute('SELECT until FROM cooldowns WHERE provider = ?', (provider.name,)).fetchone()
        if cooldown and cooldown[0] > now:
            return cooldown[0], False
        count, first, last = database.execute('SELECT COUNT(*), MIN(sent), MAX(sent) FROM requests WHERE provider = ?',
                                              (provider.name,)).fetchone()
        if count >= provider.daily_limit:
            return first + DAY, False
        if last is not None and last + provider.interval > now:
            return last + provider.interval, True
        database.execute('INSERT INTO requests VALUES (?, ?)', (provider.name, now))
    return now, False


def postpone_requests(database, provider, until):
    with database:
        database.execute("""
            INSERT INTO cooldowns VALUES (?, ?)
            ON CONFLICT (provider) DO UPDATE SET until = MAX(cooldowns.until, excluded.until)
            """, (provider.name, until))
