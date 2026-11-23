"""Keep DB-IP's open GeoIP databases on disk and answer lookups from them while they are fresh."""

from datetime import datetime, timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zlib

import mmdb


DAY = 24 * 60 * 60
MAX_AGE = 90 * DAY
# DB-IP uses monthly URLs even though we check each day.
CHECK_INTERVAL = DAY
POLL_INTERVAL = 10 * 60
# Uncompressed limit guards against a broken or hostile download filling the disk.
MAX_DOWNLOAD = 1 << 30
EDITIONS = {
    'city': ('https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz', 'City'),
    'asn': ('https://download.db-ip.com/free/dbip-asn-lite-{month}.mmdb.gz', 'ASN'),
}
LOOKUP_CACHE = 4096
VALIDATORS = {'ETag': 'If-None-Match', 'Last-Modified': 'If-Modified-Since'}


def edition_month(when):
    return datetime.fromtimestamp(when, timezone.utc).strftime('%Y-%m')


def previous_month(month):
    year, number = map(int, month.split('-'))
    return '{:04d}-{:02d}'.format(year - (number == 1), 12 if number == 1 else number - 1)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path, value):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as file:
        json.dump(value, file)
    os.replace(file.name, path)


class LocalGeoIP:
    """Serve lookups from local databases and refresh them in a background thread."""

    def __init__(self, directory, changed=None, max_age=MAX_AGE, tls_context=None, convert=None):
        self.directory = Path(directory)
        # Converts (city, asn) records once per address; cached alongside the raw lookup.
        self.convert = convert or (lambda records: records)
        self.changed = changed
        self.max_age = max_age
        self.tls_context = tls_context
        self.readers = {}
        self.loaded = {}
        self.results = {}
        self.lock = threading.Lock()
        self.failed_checks = frozenset()
        self.download_times = {}
        self.closed = threading.Event()
        self.worker = None
        self.load()
        self.load_state()

    def path(self, kind):
        return self.directory / 'dbip-{}-lite.mmdb'.format(kind)

    def fresh(self, kind, now=None):
        reader = self.readers.get(kind)
        if reader is None:
            return False
        now = time.time() if now is None else now
        if kind not in self.failed_checks or now - reader.build_epoch <= self.max_age:
            return True
        signature = self.loaded[kind]
        downloaded_file, downloaded_at = self.download_times.get(kind, (None, None))
        if downloaded_file != signature:
            # Existing and manually installed files may have no recorded download.
            downloaded_at = signature[2] / 1_000_000_000
        return now - downloaded_at <= self.max_age

    def load_state(self):
        state = read_json(self.directory / 'state.json')
        failed = frozenset(kind for kind in EDITIONS if state.get(kind + '_failed') is True)
        downloads = {}
        for kind in EDITIONS:
            signature = state.get(kind + '_file')
            downloaded_at = state.get(kind + '_downloaded')
            if isinstance(signature, list) and isinstance(downloaded_at, (int, float)):
                downloads[kind] = tuple(signature), downloaded_at
        with self.lock:
            changed = failed != self.failed_checks or downloads != self.download_times
            self.failed_checks = failed
            self.download_times = downloads
        if changed and self.changed is not None:
            self.changed.set()
        return state

    def load(self):
        """Open databases that are new on disk, including ones another process downloaded."""
        swapped = False
        for kind, (_, expected) in EDITIONS.items():
            path = self.path(kind)
            signature = None
            reader = None
            try:
                stat = path.stat()
                signature = stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size
                if self.loaded.get(kind) == signature:
                    continue
                reader = mmdb.Reader(path)
                if expected not in reader.database_type:
                    reader.close()
                    reader = None
            except (OSError, mmdb.InvalidDatabase):
                pass
            if reader is None and kind not in self.readers:
                continue
            # Readers are replaced, not closed: a lookup on another thread may still hold the old one.
            with self.lock:
                if reader is None:
                    self.readers.pop(kind, None)
                    self.loaded.pop(kind, None)
                else:
                    self.readers[kind] = reader
                    self.loaded[kind] = signature
                self.results.clear()
            swapped = True
        if swapped and self.changed is not None:
            self.changed.set()
        return swapped

    def start(self):
        if self.worker is None and not self.closed.is_set():
            self.worker = threading.Thread(target=self._update, daemon=True)
            self.worker.start()

    def close(self):
        self.closed.set()

    def lookup(self, address, stale=False):
        """Return converted (city, asn) records, or None when no fresh local location is available."""
        now = time.time()
        with self.lock:
            city, asn = self.readers.get('city'), self.readers.get('asn')
            if city is None or not (stale or self.fresh('city', now)):
                return None
            include_asn = asn is not None and (stale or self.fresh('asn', now))
            key = address, include_asn
            if key in self.results:
                return self.results[key]
        try:
            location = city.get(address)
            network = asn.get(address) if include_asn else None
        except (ValueError, IndexError, mmdb.InvalidDatabase):
            return None
        result = self.convert((location, network if isinstance(network, dict) else {})) \
            if isinstance(location, dict) else None
        with self.lock:
            if city is self.readers.get('city') and asn is self.readers.get('asn'):
                if len(self.results) >= LOOKUP_CACHE:
                    self.results.clear()
                self.results[key] = result
        return result

    def _update(self):
        while not self.closed.is_set():
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.refresh()
            except OSError:
                pass
            self.closed.wait(POLL_INTERVAL)

    def refresh(self, now=None):
        """Check each installed database daily, or fetch a missing file immediately."""
        use_wall_clock = now is None
        now = time.time() if now is None else now
        state_path = self.directory / 'state.json'
        month = edition_month(now)
        self.load()
        if not self.due_editions(self.load_state(), now):
            return
        with open(self.directory / '.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Another session is already downloading; load() picks up its result.
                return
            # Holding the lock means any partial download belongs to a session that exited early.
            for orphan in self.directory.glob('.download-*'):
                orphan.unlink(missing_ok=True)
            self.load()
            state = self.load_state()
            for kind in self.due_editions(state, now):
                if self.closed.is_set():
                    break
                edition, validators, downloaded = self.download(kind, month, state)
                state[kind + '_checked'] = now
                state[kind + '_failed'] = edition is None
                if edition is not None:
                    self.load()
                    state[kind] = edition
                    state[kind + '_validators'] = validators
                    state[kind + '_file'] = self.loaded.get(kind)
                    if downloaded:
                        state[kind + '_downloaded'] = time.time() if use_wall_clock else now
                elif kind not in self.readers:
                    state.pop(kind, None)
                    state.pop(kind + '_validators', None)
                    state.pop(kind + '_file', None)
                    state.pop(kind + '_downloaded', None)
                state.pop(kind + '_retry', None)
                write_json(state_path, state)
            self.load_state()

    def due_editions(self, state, now):
        due = []
        for kind in EDITIONS:
            checked = state.get(kind + '_checked')
            missing = kind in state and kind not in self.readers
            if missing or not isinstance(checked, (int, float)) or now - checked >= CHECK_INTERVAL:
                due.append(kind)
        return due

    def download(self, kind, month, state):
        """Check this month's edition, or last month's before publication."""
        url, expected = EDITIONS[kind]
        installed = self.loaded.get(kind)
        saved_validators = state.get(kind + '_validators')
        same_file = installed is not None and state.get(kind + '_file') == list(installed)
        for edition in (month, previous_month(month)):
            headers = {}
            if same_file and state.get(kind) == edition and isinstance(saved_validators, dict):
                headers = {conditional: saved_validators[header] for header, conditional in VALIDATORS.items()
                           if isinstance(saved_validators.get(header), str)}
            try:
                validators = self.fetch(Request(url.format(month=edition), headers=headers), self.path(kind), expected)
            except HTTPError as error:
                error.close()
                if error.code == 304 and headers:
                    return edition, saved_validators, False
                if error.code == 404:
                    continue
                return None, {}, False
            except (OSError, EOFError, zlib.error, mmdb.InvalidDatabase):
                return None, {}, False
            return edition, validators, True
        return None, {}, False

    def fetch(self, request, target, expected):
        temporary = tempfile.NamedTemporaryFile(dir=self.directory, prefix='.download-', delete=False)
        try:
            with temporary, urlopen(request, timeout=30, context=self.tls_context) as response, \
                    gzip.GzipFile(fileobj=response) as archive:
                validators = {header: response.headers[header] for header in VALIDATORS
                              if header in response.headers}
                copied = 0
                while chunk := archive.read(1 << 20):
                    if self.closed.is_set():
                        raise OSError('closed')
                    copied += len(chunk)
                    if copied > MAX_DOWNLOAD:
                        raise mmdb.InvalidDatabase('download too large')
                    temporary.write(chunk)
            reader = mmdb.Reader(temporary.name)
            try:
                if expected not in reader.database_type:
                    raise mmdb.InvalidDatabase('unexpected database type ' + reader.database_type)
            finally:
                reader.close()
            os.chmod(temporary.name, 0o600)
            os.replace(temporary.name, target)
            return validators
        except BaseException:
            Path(temporary.name).unlink(missing_ok=True)
            raise
