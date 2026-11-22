from dataclasses import replace
from email.message import Message
from io import BytesIO
from ipaddress import ip_address
from json import dumps
import os
from pathlib import Path
import socket
import sqlite3
import struct
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError

from ip_location import IPLocations, MISS_TTL, PROVIDERS, PeerDetails, RESULT_TTL, fetch_location, geo_tls_context
from ip_location_cache import DAY, LOCATION_FORMAT_VERSION, cached_location, open_cache, reserve_request, save_location
from ip_traffic import IPTraffic, PeerTraffic, interface_addresses, ip_packets
from stats_display import Dashboard, display_width


def packet(src, dst, length):
    src, dst = ip_address(src), ip_address(dst)
    if src.version == 4:
        return b'\x45\x00' + struct.pack('>H', length) + bytes(8) + src.packed + dst.packed
    return b'\x60\x00\x00\x00' + struct.pack('>H', length - 40) + bytes(2) + src.packed + dst.packed


def capture(link, packets, endian='<'):
    stream = struct.pack(endian + 'IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 96, link)
    for data in packets:
        stream += struct.pack(endian + 'IIII', 1, 0, len(data), len(data)) + data
    return BytesIO(stream)


class IPTrafficTests(unittest.TestCase):
    def test_location_api_formats_china_and_foreign_addresses(self):
        examples = (
            ('223.5.5.5', 'CN', '中国', '浙江省', '杭州市', '杭州·浙江·中国'),
            ('223.5.5.5', 'CN', '中国', '浙江省', '-', '浙江·中国'),
            ('114.114.114.114', 'CN', '中国', '北京市', '北京', '北京·中国'),
            ('202.45.32.1', 'HK', '香港', '', '香港', '香港·中国'),
            ('202.175.3.8', 'MO', '澳门', '', '澳门', '澳门·中国'),
            ('168.95.1.1', 'TW', '台湾', '', '台北', '台湾·中国'),
            ('2001:4860:4860::8888', 'US', '美国', '加利福尼亚州', '山景城', '山景城·加利福尼亚州·美国'),
            ('8.8.4.4', 'US', '美国', 'California', 'San Francisco', '旧金山·加利福尼亚州·美国'),
            ('8.8.4.4', 'US', '美国', 'California', '三藩市', '旧金山·加利福尼亚州·美国'),
            ('8.8.4.4', 'US', '美国', 'California', 'Ｓａｎ José', '圣何塞·加利福尼亚州·美国'),
            ('8.8.4.4', 'US', '美国', 'New York', 'San Francisco', 'San Francisco·纽约州·美国'),
            ('223.5.5.5', 'CN', '中国', '福建省', 'Fuzhou', '福州·福建·中国'),
            ('223.5.5.5', 'CN', '中国', '江西省', 'Fuzhou', '抚州·江西·中国'),
            ('223.5.5.5', 'CN', '中国', '陕西省', 'Xī’ān', '西安·陕西·中国'),
            ('223.5.5.5', 'CN', '中國', '廣東省', '雲浮市', '云浮·广东·中国'),
            ('223.5.5.5', 'CN', '中國', '陝西省', '乾縣', '乾县·陕西·中国'),
            ('114.114.114.114', 'CN', '中國', '重慶市', '重慶', '重庆·中国'),
            ('8.8.4.4', 'US', '美國', '加利福尼亞州', '聖荷西', '圣何塞·加利福尼亚州·美国'),
            ('8.8.4.4', 'US', '美國', '紐約州', '', '纽约州·美国'),
            ('2001:4860:4860::8888', 'US', '美国', 'California', '-', '加利福尼亚州·美国'),
            ('2001:4860:4860::8888', 'US', '美国', '-', '-', '美国'),
        )
        for address, code, country, region, city, expected in examples:
            with self.subTest(address=address, city=city), patch('ip_location.urlopen') as request:
                payload = dict(success=True, country_code=code, country=country, region=region, city=city,
                               connection={'asn': 45102, 'isp': '中華電信', 'org': 'Example Org'})
                request.return_value.__enter__.return_value.read.return_value = dumps(payload).encode()
                self.assertEqual(fetch_location(address), PeerDetails(expected, '中华电信', 'AS45102'))
                self.assertTrue(request.call_args.args[0].startswith('https://ipwho.is/' + address + '?lang=zh-CN'))
                self.assertIn('connection.asn', request.call_args.args[0])

    def test_backup_location_apis_normalize_different_response_fields(self):
        ipv4, ipv6 = '223.5.5.5', '2001:4860:4860::8888'
        examples = (
            ('FreeIPAPI', ipv4, {'ipAddress': ipv4, 'countryCode': 'CN', 'regionName': 'Zhejiang',
                                 'cityName': 'Hangzhou', 'asn': '37963', 'asnOrganization': 'Alibaba'},
             PeerDetails('杭州·浙江·中国', 'Alibaba', 'AS37963')),
            ('IPQuery', ipv6, {'ip': ipv6, 'location': {'country_code': 'CN', 'state': 'Zhejiang',
                                                      'city': 'Hangzhou'},
                              'isp': {'isp': '', 'org': 'Alibaba', 'asn': 'AS45102'}},
             PeerDetails('杭州·浙江·中国', 'Alibaba', 'AS45102')),
            ('IP2Location.io', ipv4, {'ip': ipv4, 'country_code': 'CN', 'region_name': 'Zhejiang',
                                     'city_name': 'Hangzhou', 'asn': '45102', 'as': 'Alibaba'},
             PeerDetails('杭州·浙江·中国', 'Alibaba', 'AS45102')),
            ('Country.is', ipv6, {'ip': ipv6, 'country': 'US'}, PeerDetails('美国')),
        )
        sources = {provider.name: provider for provider in PROVIDERS}
        for name, address, payload, expected in examples:
            with self.subTest(source=name), patch('ip_location.urlopen') as request:
                response = request.return_value.__enter__.return_value
                response.read.return_value = dumps(payload).encode()
                self.assertEqual(fetch_location(address, sources[name]), expected)
                request.assert_called_once_with(sources[name].url.format(address=address), timeout=3,
                                                context=geo_tls_context())

    def test_ip2location_keyless_quota_waits_for_next_utc_day(self):
        ip2location, country_is = PROVIDERS[-2:]
        now = 2 * DAY + 5400
        quota = HTTPError(ip2location.url, 401, 'Unauthorized', Message(), BytesIO())
        with TemporaryDirectory() as directory, patch('time.time', return_value=now) as clock, \
                patch('ip_location.PROVIDERS', (ip2location, country_is)), \
                patch('ip_location.fetch_location', side_effect=[quota, PeerDetails('美国'),
                                                                PeerDetails('中国')]) as request:
            locations = IPLocations(threading.Event())
            with open_cache(Path(directory) / 'location.sqlite3') as db:
                details, expires = locations._query(db, '8.8.8.8')
                self.assertEqual((details, expires), (PeerDetails('美国'), 3 * DAY))
                clock.return_value += country_is.interval
                self.assertEqual(locations._query(db, '223.5.5.5')[0], PeerDetails('中国'))
        self.assertEqual(request.call_args_list, [call('8.8.8.8', ip2location), call('8.8.8.8', country_is),
                                                  call('223.5.5.5', country_is)])

    def test_location_worker_looks_up_only_public_addresses_and_reuses_result(self):
        changed, started, release = threading.Event(), threading.Event(), threading.Event()
        details = PeerDetails('杭州·浙江·中国', 'Alibaba', 'AS45102')

        def lookup(_address, _provider):
            started.set()
            return details if release.wait(2) else PeerDetails()

        with TemporaryDirectory() as directory, patch('ip_location.fetch_location', side_effect=lookup) as request:
            cache = Path(directory) / 'location.sqlite3'
            locations = IPLocations(changed, cache)
            try:
                for address in ('10.0.0.1', '172.31.0.1', '192.168.0.1', '169.254.1.1',
                                'fe80::1', 'fd00::1', '::ffff:192.168.0.1'):
                    self.assertEqual(locations.lookup(address), PeerDetails('局域网'))
                for address in ('192.0.2.1', '100.64.0.1', 'ff02::1'):
                    self.assertEqual(locations.lookup(address), PeerDetails())
                self.assertIsNone(locations.worker)
                self.assertEqual(locations.lookup('223.5.5.5'), PeerDetails())
                self.assertTrue(started.wait(2))
                self.assertEqual(locations.lookup('::ffff:223.5.5.5'), PeerDetails())
                release.set()
                self.assertTrue(changed.wait(2))
                self.assertEqual(locations.lookup('223.5.5.5'), details)
                request.assert_called_once_with('223.5.5.5', PROVIDERS[0])
            finally:
                release.set()
                locations.close()
                if locations.worker is not None:
                    locations.worker.join(2)

            changed.clear()
            restarted = IPLocations(changed, cache)
            try:
                self.assertEqual(restarted.lookup('223.5.5.5'), PeerDetails())
                self.assertTrue(changed.wait(2))
                self.assertEqual(restarted.lookup('223.5.5.5'), details)
                request.assert_called_once_with('223.5.5.5', PROVIDERS[0])
            finally:
                restarted.close()
                restarted.worker.join(2)

    def test_location_uses_geojs_during_a_persisted_primary_cooldown(self):
        headers = Message()
        headers['Retry-After'] = '300'
        limit = HTTPError('https://ipwho.is', 429, 'Too Many Requests', headers, BytesIO())
        first, second = '223.5.5.5', '2001:4860:4860::8888'
        with TemporaryDirectory() as directory, patch('time.time', return_value=2 * DAY) as clock, \
                patch('ip_location.urlopen') as request:
            file = Path(directory) / 'location.sqlite3'
            request.side_effect = [limit, request.return_value, request.return_value]
            response = request.return_value.__enter__.return_value
            response.read.side_effect = [
                b'{"country_code":"CN","region":"Zhejiang","city":"Hangzhou","asn":45102,"organization_name":"Alibaba"}',
                b'{"country_code":"US","region":"California","city":"Mountain View","asn":15169,'
                b'"organization_name":"Google LLC"}',
            ]
            with open_cache(file) as db:
                self.assertEqual(IPLocations(threading.Event())._query(db, first),
                                 (PeerDetails('杭州·浙江·中国', 'Alibaba', 'AS45102'),
                                  clock.return_value + RESULT_TTL))
            clock.return_value += PROVIDERS[1].interval
            with open_cache(file) as db:
                self.assertEqual(IPLocations(threading.Event())._query(db, second),
                                 (PeerDetails('山景城·加利福尼亚州·美国', 'Google LLC', 'AS15169'),
                                  clock.return_value + RESULT_TTL))
            self.assertEqual([args.args[0].split('/')[2] for args in request.call_args_list],
                             ['ipwho.is', 'get.geojs.io', 'get.geojs.io'])

    def test_location_cache_expires_and_provider_quota_rolls_across_sessions(self):
        primary = replace(PROVIDERS[0], daily_limit=2)
        backup = PROVIDERS[1]
        now = 2 * DAY
        with TemporaryDirectory() as directory, patch('time.time', return_value=now) as clock:
            file = Path(directory) / 'location.sqlite3'
            with open_cache(file) as db:
                save_location(db, '8.8.8.8', '美国', 'Google LLC', 'AS15169', now + RESULT_TTL, now)
                save_location(db, '8.8.4.4', '', '', '', now + MISS_TTL, now)
                self.assertEqual(reserve_request(db, primary, now), (now, False))
                self.assertEqual(reserve_request(db, primary, now + 0.5), (now + 1, True))
                self.assertEqual(reserve_request(db, primary, now + 1), (now + 1, False))
            with open_cache(file) as db:
                self.assertEqual(reserve_request(db, primary, now + 2), (now + DAY, False))
                self.assertEqual(reserve_request(db, backup, now + 2), (now + 2, False))
                self.assertEqual(cached_location(db, '8.8.4.4', now + 2),
                                 ('', '', now + MISS_TTL, LOCATION_FORMAT_VERSION, ''))
            clock.return_value = now + DAY
            with open_cache(file) as db:
                self.assertEqual(reserve_request(db, primary, now + DAY), (now + DAY, False))
                self.assertIsNone(cached_location(db, '8.8.4.4', now + DAY))
                self.assertEqual(cached_location(db, '8.8.8.8', now + DAY),
                                 ('美国', 'Google LLC', now + RESULT_TTL, LOCATION_FORMAT_VERSION, 'AS15169'))
                self.assertIsNone(cached_location(db, '8.8.8.8', now + RESULT_TTL))

    def test_existing_location_cache_keeps_region_while_carrier_is_loaded(self):
        address, now = '8.8.8.8', 2 * DAY
        with TemporaryDirectory() as directory, patch('time.time', return_value=now):
            file = Path(directory) / 'location.sqlite3'
            db = sqlite3.connect(file)
            with db:
                db.execute('CREATE TABLE locations (address TEXT PRIMARY KEY, label TEXT NOT NULL, expires REAL)')
                db.execute('INSERT INTO locations VALUES (?, ?, ?)', (address, '美国', now + DAY))
            db.close()
            locations = IPLocations(threading.Event())

            def lookup(ip, _provider):
                self.assertEqual(locations.lookup(ip), PeerDetails('美国'))
                return PeerDetails('山景城·加利福尼亚州·美国', 'Google LLC', 'AS15169')

            with open_cache(file) as db, patch('ip_location.fetch_location', side_effect=lookup):
                locations._cached_or_query(db, address)
                self.assertEqual(cached_location(db, address, now),
                                 ('山景城·加利福尼亚州·美国', 'Google LLC', now + RESULT_TTL,
                                  LOCATION_FORMAT_VERSION, 'AS15169'))

    def test_cached_mainland_province_is_enriched_by_a_matching_city(self):
        address, now = '223.5.5.5', 2 * DAY
        expected = PeerDetails('杭州·浙江·中国', 'Alibaba')
        results = [PeerDetails('浙江·中国', 'Alibaba'),
                   PeerDetails('南京·江苏·中国', 'Other ISP'),
                   PeerDetails('杭州·浙江·中国', 'Other ISP')]
        with TemporaryDirectory() as directory, patch('time.time', return_value=now), \
                patch('ip_location.fetch_location', side_effect=results) as request:
            file = Path(directory) / 'location.sqlite3'
            with open_cache(file) as db:
                with db:
                    db.execute('INSERT INTO locations (address, label, carrier, expires) VALUES (?, ?, ?, ?)',
                               (address, '浙江·中国', 'Alibaba', now + DAY))
                details, expiry = IPLocations(threading.Event())._cached_or_query(db, address)
                self.assertEqual((details, expiry), (expected, now + RESULT_TTL))
                self.assertEqual(IPLocations(threading.Event())._cached_or_query(db, address), (expected, expiry))
            self.assertEqual(request.call_args_list, [call(address, provider) for provider in PROVIDERS[:3]])

    def test_previous_country_only_us_cache_is_enriched_with_state_and_city(self):
        address, now = '8.8.8.8', 2 * DAY
        original = PeerDetails('美国', 'Google LLC')
        details = PeerDetails('山景城·加利福尼亚州·美国', 'Google LLC')
        with TemporaryDirectory() as directory, patch('time.time', return_value=now):
            with open_cache(Path(directory) / 'location.sqlite3') as db:
                with db:
                    db.execute('INSERT INTO locations (address, label, carrier, expires, city_checked) '
                               'VALUES (?, ?, ?, ?, 1)', (address, original.location, original.carrier, now + DAY))
                locations = IPLocations(threading.Event())

                def lookup(ip, _provider):
                    self.assertEqual(locations.lookup(ip), original)
                    return details

                with patch('ip_location.fetch_location', side_effect=lookup) as request:
                    self.assertEqual(locations._cached_or_query(db, address), (details, now + RESULT_TTL))
                    self.assertEqual(IPLocations(threading.Event())._cached_or_query(db, address),
                                     (details, now + RESULT_TTL))
                    request.assert_called_once_with(address, PROVIDERS[0])

    def test_existing_location_cache_is_refreshed_once_for_asn(self):
        address, now = '223.5.5.5', 2 * DAY
        original = PeerDetails('杭州·浙江·中国', 'Alibaba')
        details = original._replace(asn='AS45102')
        with TemporaryDirectory() as directory, patch('time.time', return_value=now):
            file = Path(directory) / 'location.sqlite3'
            with open_cache(file) as database:
                with database:
                    database.execute('INSERT INTO locations (address, label, carrier, expires, city_checked) '
                                     'VALUES (?, ?, ?, ?, ?)',
                                     (address, original.location, original.carrier, now + DAY, LOCATION_FORMAT_VERSION))
                with patch('ip_location.fetch_location', return_value=details) as request:
                    for _ in range(2):
                        locations = IPLocations(threading.Event())
                        self.assertEqual(locations._cached_or_query(database, address),
                                         (details, now + RESULT_TTL))
                    request.assert_called_once_with(address, PROVIDERS[0])

    def test_cached_city_alias_is_normalized_without_another_request(self):
        now = 2 * DAY
        examples = (
            ('8.8.4.4', '三藩市·加利福尼亞州·美國', '舊金山網路', '旧金山·加利福尼亚州·美国', '旧金山网路'),
            ('223.5.5.5', '乾县·陕西·中国', '中華電信', '乾县·陕西·中国', '中华电信'),
        )
        with TemporaryDirectory() as directory, patch('time.time', return_value=now):
            with open_cache(Path(directory) / 'location.sqlite3') as database:
                with patch('ip_location.fetch_location') as request:
                    for address, label, carrier, expected_label, expected_carrier in examples:
                        with self.subTest(label=label):
                            save_location(database, address, label, carrier, 'AS15169', now + DAY, now)
                            self.assertEqual(IPLocations(threading.Event())._cached_or_query(database, address),
                                             (PeerDetails(expected_label, expected_carrier, 'AS15169'), now + DAY))
                    request.assert_not_called()

    def test_location_appears_without_changing_pagination(self):
        addresses = ('2607:f8b0:4004:c07::71', '223.5.5.5', '202.45.32.1', '8.8.8.8')
        locations = {}
        capture = SimpleNamespace(interface='en0', status='', hostname=lambda _: 'dns.example',
                                  peer_details=lambda address: locations.get(address, PeerDetails()),
                                  rows=[PeerTraffic(address, 100, 200, 1, 2) for address in addresses])
        for columns, budget in ((132, 5), (84, 7), (40, 7)):
            with self.subTest(columns=columns):
                display = Dashboard(columns, color=True, truecolor=True, theme='dark')
                before_targets, after_targets = [], []
                _, paging = display.ip_panel(capture, columns, budget, 0, click_targets=before_targets)
                locations[addresses[0]] = PeerDetails('美国', 'Google', 'AS15169')
                lines, new_paging = display.ip_panel(capture, columns, budget, 0, click_targets=after_targets)
                locations.clear()
                self.assertEqual((new_paging, after_targets), (paging, before_targets))
                self.assertIn(addresses[0], lines[2])
                row = lines[2 if columns == 132 else 3]
                self.assertLess(row.index('美国'), row.index('Google'))
                self.assertIn(display.paint('  Google', 'peer_carrier') +
                              display.paint('  AS15169', 'peer_asn') +
                              display.paint('  dns.example', 'peer_name'), row)
                self.assertTrue(all(display_width(line) == columns for line in lines))

    def test_ip_table_shortens_carrier_names_before_clipping(self):
        address = '223.5.5.5'
        capture = SimpleNamespace(interface='en0', status='', hostname=lambda _: 'dns.example',
                                  rows=[PeerTraffic(address, 100, 200, 1, 2)])
        examples = (('AS9808 CHINA_MOBILE Communications Group Co., Ltd.', '中国移动'),
                    ('China Unicom China169 Backbone Network', '中国联通'),
                    ('CHINANET Guangdong Province Network', '中国电信'),
                    ('Google LLC', 'Google LLC'))
        for raw, label in examples:
            with self.subTest(carrier=raw):
                capture.peer_details = lambda _: PeerDetails('杭州·浙江·中国', raw)
                lines, _ = Dashboard(46).ip_panel(capture, 46, 7, 0)
                self.assertIn('杭州·浙江·中国  ' + label + '  dns.example', lines[3])

    def test_ip_table_omits_us_state_suffix(self):
        capture = SimpleNamespace(interface='en0', status='', rows=[PeerTraffic('8.8.8.8', 100, 200, 1, 2)])
        examples = (
            ('旧金山·加利福尼亚州·美国', '旧金山·加利福尼亚·美国'),
            ('纽约州·美国', '纽约·美国'),
            ('华盛顿·华盛顿特区·美国', '华盛顿·华盛顿特区·美国'),
            ('杭州·浙江·中国', '杭州·浙江·中国'),
            ('美国', '美国'),
        )
        for label, expected in examples:
            with self.subTest(label=label):
                capture.peer_details = lambda _: PeerDetails(label)
                lines, _ = Dashboard(84).ip_panel(capture, 84, 7, 0)
                self.assertEqual(lines[3].strip('│ '), expected)

    def test_decodes_snapshots_with_full_lengths_on_supported_links(self):
        ip = packet('192.0.2.1', '192.0.2.2', 1400)
        for link, head in ((0, b'\x02\x00\x00\x00'), (108, b'\x00\x00\x00\x02'),
                           (101, b''), (1, bytes(12) + b'\x81\x00\x00\x01\x08\x00'),
                           (113, bytes(14) + b'\x08\x00'), (276, b'\x08\x00' + bytes(18))):
            with self.subTest(link=link):
                self.assertEqual(list(ip_packets(capture(link, [head + ip], '>'))),
                                 [(ip_address('192.0.2.1').packed, ip_address('192.0.2.2').packed, 1400)])

    def test_session_aggregates_by_peer_and_zeros_only_the_idle_rates(self):
        local = ('192.0.2.1', '2001:db8::1')
        peer = ('192.0.2.2', '2001:db8::2')
        traffic = IPTraffic('en0', threading.Event())
        traffic.process = Mock()
        traffic.process.stdout = capture(101, [
            packet(local[0], peer[0], 1400), packet(peer[0], local[0], 300),
            packet(peer[0], local[0], 200), packet(peer[1], local[1], 1000),
            packet(peer[0], '198.51.100.8', 700)])
        traffic.process.stderr = BytesIO(b'tcpdump: listening on en0\n')
        with patch('ip_traffic.interface_addresses', return_value=({ip_address(a).packed for a in local}, set())):
            traffic._read()
        traffic.previous = 10
        with patch('ip_traffic.time.monotonic', side_effect=(10.25, 10.35)):
            traffic.sample()
            first, second = traffic.rows
            self.assertEqual((first.address, first.upload, first.download), (peer[0], 1400, 500))
            self.assertAlmostEqual(first.up_mbps, 1400 * 8 / 250000)
            self.assertAlmostEqual(first.down_mbps, 500 * 8 / 250000)
            self.assertEqual((second.address, second.upload, second.download), (peer[1], 0, 1000))
            traffic.sample()
            self.assertEqual(traffic.rows[0].down_mbps, 0)
            self.assertEqual(traffic.rows[0].download, 500)
        for cols, rows in ((120, 35), (58, 15), (30, 6), (13, 2)):
            with patch.object(traffic, 'hostname', return_value=''):
                view = Dashboard(cols, interval=0.1).ip_screen(traffic, rows, 1)
            self.assertEqual(len(view.splitlines()), rows)
            self.assertTrue(all(display_width(line) <= cols for line in view.splitlines()))
            self.assertIn('0.1s', view)

    def test_reverse_dns_is_asynchronous_and_caches_both_outcomes(self):
        ipv4, ipv6, missing = '192.0.2.1', '2001:db8::1', '192.0.2.2'
        started, release = threading.Event(), threading.Event()
        traffic = IPTraffic('en0', threading.Event())

        def lookup(address):
            if address == missing:
                raise socket.herror(1, 'Host not found')
            if address == ipv4:
                started.set()
                if not release.wait(2):
                    raise TimeoutError
                return 'router.example.', [], [address]
            return 'ipv6.example', [], [address]

        with patch('ip_traffic.socket.gethostbyaddr', side_effect=lookup) as resolver:
            try:
                self.assertEqual(traffic.hostname(missing), '')
                self.assertEqual(traffic.hostname(ipv4), '')
                self.assertTrue(started.wait(2))
                self.assertEqual(traffic.hostname(missing), '')
                self.assertEqual(traffic.hostname(ipv4), '')
                self.assertFalse(traffic.changed.is_set())
                release.set()
                self.assertTrue(traffic.changed.wait(2))
                self.assertEqual(traffic.hostname(ipv4), 'router.example')
                traffic.changed.clear()
                self.assertEqual(traffic.hostname(ipv6), '')
                self.assertTrue(traffic.changed.wait(2))
                self.assertEqual(traffic.hostname(ipv6), 'ipv6.example')
                self.assertEqual(resolver.call_args_list, [call(missing), call(ipv4), call(ipv6)])
            finally:
                release.set()
                traffic.close()
                if traffic.hostname_worker is not None:
                    traffic.hostname_worker.join(2)
            self.assertFalse(traffic.hostname_worker.is_alive())

    def test_names_fit_without_changing_peer_layout(self):
        addresses = ('192.0.2.2', '2001:db8::2', '192.0.2.3')
        names = {}
        capture = SimpleNamespace(interface='en0', status='', hostname=lambda address: names.get(address, ''),
                                  rows=[PeerTraffic(address, 100, 200, 1, 2) for address in addresses])
        hostname = 'router.with.a.long.hostname.example'
        for cols, budget, shown_name in ((88, 5, 'router.with.a.long.h…'), (30, 9, 'router.with.a.…')):
            with self.subTest(columns=cols):
                display = Dashboard(cols, color=True)
                before_targets, after_targets = [], []
                before, pages = display.ip_panel(capture, cols, budget, 0, click_targets=before_targets)
                names[addresses[0]] = hostname
                after, new_pages = display.ip_panel(capture, cols, budget, 0, click_targets=after_targets)
                names.clear()

                self.assertEqual((new_pages, after_targets), (pages, before_targets))
                peer_line = after[2]
                self.assertIn(display.paint(addresses[0], 'ink') +
                              display.paint('  ' + shown_name, 'peer_name'), peer_line)
                self.assertEqual(display_width(peer_line), cols)
                self.assertEqual(after[:2] + after[3:], before[:2] + before[3:])

    def test_capture_readiness_precedes_packets_and_denial_stays_hidden(self):
        with patch('ip_traffic.interface_addresses', return_value=(set(), set())):
            traffic = IPTraffic('en0', threading.Event())
            traffic.process = Mock(returncode=0, stderr=BytesIO(b'tcpdump: listening on en0\n'))
            read_fd, write_fd = os.pipe()
            with os.fdopen(write_fd, 'wb') as writer:
                traffic.process.stdout = os.fdopen(read_fd, 'rb')
                worker = threading.Thread(target=traffic._read, daemon=True)
                worker.start()
                try:
                    self.assertTrue(traffic.changed.wait(2))
                    self.assertTrue(traffic.ready)
                    writer.write(capture(101, []).getvalue())
                finally:
                    writer.close()
                    worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(traffic.ready)

            denied = IPTraffic('en0', threading.Event())
            denied.process = Mock(returncode=1, stdout=BytesIO(),
                                  stderr=BytesIO(b'tcpdump: en0: Permission denied\n'))
            denied._read()
            self.assertFalse(denied.ready)
            self.assertEqual(denied.status, '当前用户没有此网卡的抓包权限')

    def test_mac_and_linux_local_addresses_include_ipv6(self):
        darwin = 'en0: flags=8863\n inet 192.0.2.1 netmask 0xffffff00 broadcast 192.0.2.255\n inet6 fe80::1%en0 prefixlen 64'
        linux = '[{"addr_info":[{"local":"192.0.2.1","broadcast":"192.0.2.255"},{"local":"fe80::1"}]}]'
        for system, output in (('darwin', darwin), ('linux', linux)):
            with patch('ip_traffic.sys.platform', system), patch('ip_traffic.subprocess.check_output', return_value=output):
                self.assertEqual(interface_addresses('en0'), ({ip_address(a).packed for a in ('192.0.2.1', 'fe80::1')},
                                                             {ip_address('192.0.2.255').packed}))
