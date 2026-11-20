from io import BytesIO
from ipaddress import ip_address
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from ip_traffic import IPTraffic, interface_addresses, ip_packets
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
        traffic.process.communicate.return_value = b'', b''
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
            view = Dashboard(cols, interval=0.1).ip_screen(traffic, rows, 1)
            self.assertEqual(len(view.splitlines()), rows)
            self.assertTrue(all(display_width(line) <= cols for line in view.splitlines()))
            self.assertIn('0.1s', view)

    def test_mac_and_linux_local_addresses_include_ipv6(self):
        darwin = 'en0: flags=8863\n inet 192.0.2.1 netmask 0xffffff00 broadcast 192.0.2.255\n inet6 fe80::1%en0 prefixlen 64'
        linux = '[{"addr_info":[{"local":"192.0.2.1","broadcast":"192.0.2.255"},{"local":"fe80::1"}]}]'
        for system, output in (('darwin', darwin), ('linux', linux)):
            with patch('ip_traffic.sys.platform', system), patch('ip_traffic.subprocess.check_output', return_value=output):
                self.assertEqual(interface_addresses('en0'), ({ip_address(a).packed for a in ('192.0.2.1', 'fe80::1')},
                                                             {ip_address('192.0.2.255').packed}))
