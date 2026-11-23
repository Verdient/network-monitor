import gzip
from io import BytesIO
from ipaddress import ip_network
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import fcntl

import geoip_local
from geoip_local import DAY, LocalGeoIP, edition_month
from ip_location import IPLocations, PeerDetails, local_details
import mmdb


def header(kind, size):
    if size < 29:
        marker, extra = size, b''
    elif size < 285:
        marker, extra = 29, bytes([size - 29])
    elif size < 65821:
        marker, extra = 30, (size - 285).to_bytes(2, 'big')
    else:
        marker, extra = 31, (size - 65821).to_bytes(3, 'big')
    if kind <= 7:
        return bytes([kind << 5 | marker]) + extra
    return bytes([marker, kind - 7]) + extra


def encode(value):
    if isinstance(value, str):
        raw = value.encode()
        return header(2, len(raw)) + raw
    if isinstance(value, bool):
        return header(14, int(value))
    if isinstance(value, int):
        raw = value.to_bytes((value.bit_length() + 7) // 8, 'big')
        return header(6 if value < 2 ** 32 else 9, len(raw)) + raw
    if isinstance(value, float):
        return header(3, 8) + struct.pack('>d', value)
    if isinstance(value, dict):
        return header(7, len(value)) + b''.join(encode(k) + encode(v) for k, v in value.items())
    if isinstance(value, list):
        return header(11, len(value)) + b''.join(map(encode, value))
    raise TypeError(value)


def build_mmdb(networks, database_type='DBIP-City-Lite', build_epoch=None):
    """Build an IPv6 MaxMind DB with 24-bit records; IPv4 networks live under ::/96."""
    nodes, data = [[None, None]], b''
    for network, record in networks:
        net = ip_network(network)
        prefix = net.prefixlen + (96 if net.version == 4 else 0)
        value, offset = int(net.network_address), len(data)
        data += encode(record)
        node = 0
        for depth in range(prefix):
            bit = (value >> (127 - depth)) & 1
            if depth == prefix - 1:
                nodes[node][bit] = ('data', offset)
            else:
                if nodes[node][bit] is None:
                    nodes.append([None, None])
                    nodes[node][bit] = ('node', len(nodes) - 1)
                node = nodes[node][bit][1]
    count = len(nodes)

    def record(entry):
        if entry is None:
            return count
        return entry[1] if entry[0] == 'node' else count + 16 + entry[1]

    tree = b''.join(record(left).to_bytes(3, 'big') + record(right).to_bytes(3, 'big') for left, right in nodes)
    metadata = {'node_count': count, 'record_size': 24, 'ip_version': 6, 'database_type': database_type,
                'build_epoch': int(time.time() if build_epoch is None else build_epoch),
                'binary_format_major_version': 2, 'binary_format_minor_version': 0}
    return tree + bytes(16) + data + mmdb.METADATA_MARKER + encode(metadata)


CITY = [
    ('8.8.8.0/24', {'country': {'iso_code': 'US', 'names': {'en': 'United States', 'zh-CN': '美国'}},
                    'subdivisions': [{'names': {'en': 'California'}}],
                    'city': {'names': {'en': 'San Francisco'}}, 'location': {'latitude': 37.7}}),
    ('2001:4860::/32', {'country': {'iso_code': 'CN', 'names': {'en': 'China'}},
                        'subdivisions': [{'names': {'en': 'Zhejiang'}}], 'city': {'names': {'en': 'Hangzhou'}}}),
    ('1.1.1.0/24', {'continent': {'code': 'OC'}}),
    ('202.96.134.0/24', {'country': {'iso_code': 'CN'}, 'subdivisions': [{'names': {'en': 'Guangdong'}}],
                         'city': {'names': {'en': 'Guangzhou (Yuexiu Qu)'}}}),
]
ASN = [('8.8.8.0/24', {'autonomous_system_number': 15169, 'autonomous_system_organization': 'Google LLC'})]


def install(directory, kind, networks, **options):
    database_type = 'DBIP-City-Lite' if kind == 'city' else 'DBIP-ASN-Lite'
    path = Path(directory) / 'dbip-{}-lite.mmdb'.format(kind)
    path.write_bytes(build_mmdb(networks, database_type, **options))
    return path


class ReaderTests(unittest.TestCase):
    def test_reader_finds_ipv4_ipv6_and_mapped_addresses(self):
        with TemporaryDirectory() as directory:
            reader = mmdb.Reader(install(directory, 'city', CITY))
            self.assertEqual(reader.get('8.8.8.8')['city']['names']['en'], 'San Francisco')
            self.assertEqual(reader.get('::ffff:8.8.8.8')['country']['iso_code'], 'US')
            self.assertEqual(reader.get('2001:4860:4860::8888')['country']['iso_code'], 'CN')
            self.assertEqual(reader.get('8.8.8.8')['location']['latitude'], 37.7)
            self.assertIsNone(reader.get('9.9.9.9'))
            self.assertIn('City', reader.database_type)
            reader.close()

    def test_reader_rejects_files_without_metadata(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.mmdb'
            path.write_bytes(b'not a database' * 10)
            with self.assertRaises(mmdb.InvalidDatabase):
                mmdb.Reader(path)


class LocalLookupTests(unittest.TestCase):
    def test_fresh_local_data_answers_without_the_apis(self):
        with TemporaryDirectory() as directory:
            install(directory, 'city', CITY)
            install(directory, 'asn', ASN)
            geoip = LocalGeoIP(directory, convert=local_details)
            locations = IPLocations(threading.Event(), geoip=geoip)
            with patch.object(geoip, 'start'), patch('ip_location.fetch_location') as request:
                self.assertEqual(locations.lookup('8.8.8.8'),
                                 PeerDetails('旧金山·加利福尼亚州·美国', 'Google LLC', 'AS15169'))
                self.assertEqual(locations.lookup('2001:4860:4860::8888'), PeerDetails('杭州·浙江·中国'))
                self.assertEqual(locations.lookup('202.96.134.133'), PeerDetails('广州·广东·中国'))
                request.assert_not_called()
            locations.close()

    def test_missing_local_record_falls_back_to_the_apis(self):
        with TemporaryDirectory() as directory:
            install(directory, 'city', CITY)
            geoip = LocalGeoIP(directory, convert=local_details)
            locations = IPLocations(threading.Event(), Path(directory) / 'cache.sqlite3', geoip=geoip)
            with patch.object(geoip, 'start'), patch.object(locations, '_lookup_online',
                                                            return_value=PeerDetails('日本')) as online:
                # A record without a country is as good as no record.
                self.assertEqual(locations.lookup('1.1.1.1'), PeerDetails('日本'))
                self.assertEqual(locations.lookup('9.9.9.9'), PeerDetails('日本'))
                self.assertEqual(online.call_count, 2)

    def test_outdated_local_data_yields_to_the_apis_but_fills_gaps(self):
        with TemporaryDirectory() as directory:
            now = time.time()
            old = now - 91 * DAY
            path = install(directory, 'city', CITY, build_epoch=old)
            (Path(directory) / 'state.json').write_text('{"city_failed": true}')
            geoip = LocalGeoIP(directory, convert=local_details)
            self.assertTrue(geoip.fresh('city'))
            os.utime(path, (old, old))
            geoip.load()
            self.assertFalse(geoip.fresh('city'))
            locations = IPLocations(threading.Event(), geoip=geoip)
            with patch.object(geoip, 'start'), patch.object(locations, '_lookup_online',
                                                            side_effect=[PeerDetails('德国'), PeerDetails()]):
                self.assertEqual(locations.lookup('8.8.8.8'), PeerDetails('德国'))
                self.assertEqual(locations.lookup('8.8.8.8'), PeerDetails('旧金山·加利福尼亚州·美国'))

    def test_cached_hits_respect_city_and_asn_expiration(self):
        now = int(time.time())
        with TemporaryDirectory() as directory:
            city = install(directory, 'city', CITY, build_epoch=now)
            asn = install(directory, 'asn', ASN, build_epoch=now - 2 * DAY)
            os.utime(city, (now, now))
            os.utime(asn, (now - 2 * DAY, now - 2 * DAY))
            (Path(directory) / 'state.json').write_text('{"city_failed": true, "asn_failed": true}')
            geoip = LocalGeoIP(directory, convert=local_details)
            locations = IPLocations(threading.Event(), geoip=geoip)
            with patch('geoip_local.time.time', return_value=now) as clock, \
                    patch.object(geoip, 'start'), patch.object(locations, '_lookup_online',
                                                              return_value=PeerDetails('德国')) as online:
                self.assertEqual(locations.lookup('8.8.8.8').asn, 'AS15169')
                clock.return_value = now + 88 * DAY
                self.assertEqual(locations.lookup('8.8.8.8').asn, 'AS15169')
                clock.return_value = now + 89 * DAY
                self.assertEqual(locations.lookup('8.8.8.8').asn, '')
                online.assert_not_called()
                clock.return_value = now + 91 * DAY
                self.assertEqual(locations.lookup('8.8.8.8'), PeerDetails('德国'))
                self.assertEqual(geoip.lookup('8.8.8.8', stale=True).asn, 'AS15169')

    def test_new_files_on_disk_replace_loaded_databases(self):
        with TemporaryDirectory() as directory:
            changed = threading.Event()
            geoip = LocalGeoIP(directory, changed, convert=local_details)
            self.assertIsNone(geoip.lookup('8.8.8.8'))
            install(directory, 'city', CITY)
            self.assertTrue(geoip.load())
            self.assertTrue(changed.is_set())
            self.assertEqual(geoip.lookup('8.8.8.8').location, '旧金山·加利福尼亚州·美国')
            self.assertFalse(geoip.load())


def archive(networks, database_type):
    return gzip.compress(build_mmdb(networks, database_type))


def not_found(url):
    return HTTPError(url, 404, 'Not Found', None, BytesIO())


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.now = time.mktime((2026, 11, 23, 12, 0, 0, 0, 0, -1))
        self.month = edition_month(self.now)

    def serve(self, files, etag=None):
        def urlopen(request, **_):
            url = request.full_url
            for name, payload in files.items():
                if name in url:
                    if etag and request.get_header('If-none-match') == etag:
                        raise HTTPError(url, 304, 'Not Modified', None, BytesIO())
                    response = BytesIO(payload)
                    response.headers = {'ETag': etag} if etag else {}
                    return response
            raise not_found(url)
        return patch('geoip_local.urlopen', side_effect=urlopen)

    def test_daily_checks_reuse_unchanged_files(self):
        files = {'city-lite-2026-11': archive(CITY, 'DBIP-City-Lite'),
                 'asn-lite-2026-11': archive(ASN, 'DBIP-ASN-Lite')}
        with TemporaryDirectory() as directory, self.serve(files, etag='"edition"') as request:
            geoip = LocalGeoIP(directory, convert=local_details)
            geoip.refresh(self.now)
            self.assertEqual(request.call_count, 2)
            state = json.loads((Path(directory) / 'state.json').read_text())
            self.assertEqual((state['city'], state['asn']), ('2026-11', '2026-11'))
            self.assertEqual(geoip.lookup('8.8.8.8').carrier, 'Google LLC')
            signature = geoip.path('city').stat().st_ino
            geoip.refresh(self.now + DAY - 1)
            self.assertEqual(request.call_count, 2)
            geoip.refresh(self.now + DAY)
            self.assertEqual(request.call_count, 4)
            self.assertEqual(request.call_args.args[0].get_header('If-none-match'), '"edition"')
            self.assertEqual(geoip.path('city').stat().st_ino, signature)
            state = json.loads((Path(directory) / 'state.json').read_text())
            self.assertEqual(state['city_downloaded'], self.now)
            self.assertEqual(state['city_checked'], self.now + DAY)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                             ['.lock', 'dbip-asn-lite.mmdb', 'dbip-city-lite.mmdb', 'state.json'])

    def test_missing_and_empty_files_are_replaced_in_the_same_month(self):
        files = {'city-lite-' + self.month: archive(CITY, 'DBIP-City-Lite'),
                 'asn-lite-' + self.month: archive(ASN, 'DBIP-ASN-Lite')}
        with TemporaryDirectory() as directory, self.serve(files) as request:
            local = LocalGeoIP(directory, convert=local_details)
            local.refresh(self.now)
            self.assertIsNotNone(local.lookup('8.8.8.8'))
            local.path('city').unlink()
            local.path('asn').unlink()
            local.path('asn').touch()
            self.assertTrue(local.load())
            self.assertIsNone(local.lookup('8.8.8.8'))
            local.refresh(self.now)
            self.assertEqual(request.call_count, 4)
            self.assertEqual(local.lookup('8.8.8.8').asn, 'AS15169')

    def test_unpublished_month_falls_back_and_checks_again_later(self):
        files = {'city-lite-2026-10': archive(CITY, 'DBIP-City-Lite'),
                 'asn-lite-2026-10': archive(ASN, 'DBIP-ASN-Lite')}
        with TemporaryDirectory() as directory, self.serve(files) as request:
            geoip = LocalGeoIP(directory, convert=local_details)
            geoip.refresh(self.now)
            self.assertEqual(request.call_count, 4)
            self.assertEqual(geoip.lookup('8.8.8.8').location, '旧金山·加利福尼亚州·美国')
            geoip.refresh(self.now + 60)
            self.assertEqual(request.call_count, 4)
            state = json.loads((Path(directory) / 'state.json').read_text())
            self.assertEqual(state['city'], '2026-10')
            self.assertEqual(state['city_checked'], self.now)
            self.assertFalse(state['city_failed'])

    def test_invalid_download_keeps_the_current_database(self):
        files = {'city-lite-2026-11': archive(ASN, 'DBIP-ASN-Lite'), 'asn-lite-2026-11': b'not gzip'}
        with TemporaryDirectory() as directory, self.serve(files):
            path = install(directory, 'city', CITY, build_epoch=self.now - 40 * DAY)
            before = path.read_bytes()
            LocalGeoIP(directory).refresh(self.now)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(directory).glob('.download-*')], [])
            state = json.loads((Path(directory) / 'state.json').read_text())
            self.assertEqual(state['city_checked'], self.now)
            self.assertTrue(state['city_failed'])

    def test_old_database_remains_usable_until_its_download_also_ages(self):
        files = {'city-lite-2026-11': gzip.compress(build_mmdb(CITY, build_epoch=self.now - 91 * DAY)),
                 'asn-lite-2026-11': archive(ASN, 'DBIP-ASN-Lite')}
        with TemporaryDirectory() as directory, self.serve(files, etag='"edition"'):
            geoip = LocalGeoIP(directory)
            geoip.refresh(self.now)
            self.assertTrue(geoip.fresh('city', self.now))
            with patch('geoip_local.urlopen', side_effect=TimeoutError) as offline:
                geoip.refresh(self.now + DAY)
                self.assertEqual(offline.call_count, 2)
                self.assertTrue(geoip.fresh('city', self.now + DAY))
                self.assertTrue(geoip.fresh('asn', self.now + DAY))
                geoip.refresh(self.now + DAY + 60)
                self.assertEqual(offline.call_count, 2)
                geoip.refresh(self.now + 90 * DAY)
                self.assertTrue(geoip.fresh('city', self.now + 90 * DAY))
                geoip.refresh(self.now + 91 * DAY)
                self.assertFalse(geoip.fresh('city', self.now + 91 * DAY))
                self.assertFalse(LocalGeoIP(directory).fresh('city', self.now + 91 * DAY))
            recovered_at = self.now + 92 * DAY
            recovered_month = edition_month(recovered_at)
            with self.serve({'city-lite-' + recovered_month: files['city-lite-2026-11']}):
                geoip.refresh(recovered_at)
            self.assertTrue(geoip.fresh('city', recovered_at))
            state = json.loads((Path(directory) / 'state.json').read_text())
            self.assertEqual(state['city_downloaded'], recovered_at)

    def test_another_session_downloading_skips_the_check(self):
        with TemporaryDirectory() as directory, self.serve({}) as request, \
                open(Path(directory) / '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            LocalGeoIP(directory).refresh(self.now)
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
