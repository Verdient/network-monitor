import sys
import unittest
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stats_display import Dashboard, display_width
from tcp_connections import Connections, ConnectionReader, PortUsage, TCPSocket, linux_sockets, port_range, read_connections


class SocketTableTests(unittest.TestCase):
    def test_linux_combines_both_families_and_groups_ports_by_established_peer(self):
        def encode_words(address):
            return ''.join(int.from_bytes(address[i:i + 4], sys.byteorder).to_bytes(4, 'big').hex()
                           for i in range(0, len(address), 4))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / 'net'
            local.mkdir()
            ipv4 = bytes((192, 0, 2, 4))
            remote = encode_words(ipv4)
            (local / 'tcp').write_text('sl local_address rem_address st\n' + '\n'.join(
                '0: 00000000:1234 {}:0001 {}'.format(remote, state) for state in ('01', '0A', '06', '03')))
            mapped = encode_words(bytes(10) + bytes.fromhex('ffff') + ipv4)
            (local / 'tcp6').write_text('sl local_address remote_address st\n0: ' + '0' * 32 +
                                        ':1234 ' + mapped + ':0002 01\n')
            with patch('tcp_connections.sys.platform', 'linux'), patch('tcp_connections.linux_sockets',
                                                                       return_value=linux_sockets(root)):
                self.assertEqual(read_connections(), Connections(2, 1, 1, 1, (('192.0.2.4', 2),)))
            (local / 'tcp6').unlink()
            self.assertEqual(len(linux_sockets(root)[0]), 4)

    def test_linux_counts_shared_namespace_tables_once_and_reports_unreadable_tables(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / 'net'
            container = root / '10/net'
            shared = root / '11/net'
            unreadable = root / '12/net'
            for table_directory in (local, container, shared, unreadable):
                table_directory.mkdir(parents=True)
            header = 'sl local_address rem_address st\n'
            (local / 'tcp').write_text(header + '0: 00000000:1234 00000000:0001 06\n')
            (container / 'tcp').write_text(header + '0: 00000000:1234 00000000:0001 0A\n')
            (container / 'tcp6').write_text(header + '0: ' + '0' * 32 + ':5678 ' + '0' * 32 + ':0002 0A\n')
            (shared / 'tcp').hardlink_to(container / 'tcp')
            (shared / 'tcp6').hardlink_to(container / 'tcp6')
            (unreadable / 'tcp').write_text('invalid table')
            (root / '13').mkdir()  # The process disappeared before its TCP tables were read.
            with patch('tcp_connections.sys.platform', 'linux'), patch('tcp_connections.linux_sockets',
                                                                       return_value=linux_sockets(root)):
                self.assertEqual(read_connections(), Connections(listening=2, time_wait=1, partial=True))

    def test_darwin_reads_numeric_ipv4_and_scoped_ipv6(self):
        output = '''Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4  0      0       192.0.2.1.54321        192.0.2.7.443          ESTABLISHED
tcp6  0      0       fe80::1%en0.54320      fe80::2%en0.443        ESTABLISHED
tcp46 0      0       *.80                   *.*                    LISTEN
tcp4  0      0       192.0.2.1.7777         192.0.2.7.9000         TIME_WAIT
'''
        with patch('tcp_connections.sys.platform', 'darwin'), patch('tcp_connections.subprocess.check_output',
                                                                    return_value=output) as netstat:
            self.assertEqual(read_connections(), Connections(2, 1, 1, 0, (('192.0.2.7', 1), ('fe80::2%en0', 1))))
            netstat.return_value = ''
            self.assertEqual(read_connections(), Connections())
            netstat.side_effect = PermissionError()
            self.assertEqual(read_connections().status, '连接表不可用')

    def test_proxy_ports_are_counted_per_local_address_and_target(self):
        primary, secondary, excluded = '192.0.2.1', '192.0.2.2', '192.0.2.3'
        target_a, target_b = ('203.0.113.10', 443), ('203.0.113.10', 80)
        client = '198.51.100.2'
        namespace = (1, 42)
        sockets = [
            TCPSocket(('0.0.0.0', 40004), ('0.0.0.0', 0), 'LISTEN', 4),
            TCPSocket((primary, 1080), ('0.0.0.0', 0), 'LISTEN', 4),
            TCPSocket((primary, 1080), (client, 51000), 'ESTABLISHED', 4),
            TCPSocket((primary, 1080), (client, 51001), 'ESTABLISHED', 4),
            TCPSocket((primary, 40000), target_a, 'ESTABLISHED', 4),
            TCPSocket((primary, 40000), target_b, 'ESTABLISHED', 4),
            TCPSocket((primary, 40001), target_a, 'TIME_WAIT', 4),
            TCPSocket((primary, 40003), target_a, 'SYN_SENT', 4),
            TCPSocket((primary, 44000), target_b, 'ESTABLISHED', 4),
            TCPSocket((secondary, 40000), target_b, 'ESTABLISHED', 4),
            TCPSocket((excluded, 40000), target_b, 'ESTABLISHED', 4),
            TCPSocket((primary, 40000), target_a, 'ESTABLISHED', 4, namespace),
        ]
        hints = {}
        addresses = {ip_address(address).packed for address in (primary, secondary)}
        with patch('tcp_connections.sys.platform', 'linux'), \
                patch('tcp_connections.linux_sockets', return_value=(sockets, True)), \
                patch('tcp_connections.ephemeral_ports', return_value=port_range(40000, 40005, '40002')), \
                patch('ip_traffic.interface_addresses', return_value=(addresses, set())):
            snapshot = read_connections('eth0', hints)

        self.assertEqual(snapshot.established, 8)
        first, second, foreign = snapshot.local
        self.assertEqual((first.address, second.address, foreign.namespace), (primary, secondary, namespace))
        self.assertEqual((first.outgoing, first.incoming, first.incoming_peers, first.unknown), (2, 2, 1, 1))
        self.assertEqual((first.ports, first.new_target_remaining), (PortUsage(4, 5), 4))
        hot, other = first.targets
        self.assertEqual((hot.endpoint, hot.usage, hot.time_wait), (target_a, PortUsage(4, 5), 1))
        self.assertEqual((other.endpoint, other.usage), (target_b, PortUsage(2, 5)))
        self.assertEqual(second.ports, PortUsage(2, 5))
        self.assertEqual(foreign.unknown, 1)
        self.assertIsNone(foreign.ports)
        self.assertIn((None, (primary, 40003), target_a), hints)

        display = Dashboard(116)
        targets = []
        overview = display.connection_screen((snapshot, ()), 18, 0, targets)
        self.assertIn('本地地址总览', overview)
        self.assertIn('命名空间 42', overview)
        self.assertEqual(list(dict.fromkeys(action for _, _, _, action in targets if isinstance(action, tuple))),
                         [('tcp-address', None, primary), ('tcp-address', None, secondary),
                          ('tcp-address', namespace, primary)])
        screen = display.connection_screen((snapshot, ()), 18, 0, selected=(None, primary))
        self.assertIn('203.0.113.10:443', screen)
        self.assertIn('4/5 80.0%', screen)
        target = next(line for line in screen.splitlines() if '203.0.113.10:443' in line)
        self.assertRegex(target, r'4/5 80\.0%\s+1\s+1\s*│$')
        self.assertRegex(screen, r'入站对端 +1')
        self.assertEqual(display.paging, (0, 1))
        narrow = Dashboard(34)
        screen = narrow.connection_screen((snapshot, ()), 15, 99, selected=(namespace, primary))
        self.assertIn('命名空间 42', screen)
        self.assertIn('临时端口容量未知', screen)
        self.assertTrue(all(display_width(line) <= 34 for line in screen.splitlines()))

    def test_direction_from_handshake_survives_a_listener_change(self):
        address = '192.0.2.1'
        local, remote = (address, 40001), ('198.51.100.2', 51000)
        hints = {}
        frames = [
            ([TCPSocket(local, remote, 'SYN_RECV', 4)], False),
            ([TCPSocket(local, remote, 'ESTABLISHED', 4)], False),
            ([], False),
            ([TCPSocket(local, remote, 'ESTABLISHED', 4)], False),
        ]
        with patch('tcp_connections.sys.platform', 'linux'), \
                patch('tcp_connections.linux_sockets', side_effect=frames), \
                patch('tcp_connections.ephemeral_ports', return_value=port_range(40000, 40005)), \
                patch('ip_traffic.interface_addresses', return_value=({ip_address(address).packed}, set())):
            groups = [read_connections('eth0', hints).local[0] for _ in frames]
        self.assertEqual([(group.incoming, group.outgoing) for group in groups], [(0, 0), (1, 0), (0, 0), (0, 1)])

    def test_background_reader_publishes_complete_snapshots_and_marks_gaps(self):
        reader = ConnectionReader()
        with patch('tcp_connections.read_connections', side_effect=(Connections(7), Connections(status='连接表不可用'))):
            for expected in ((7,), (7, None)):
                reader.refresh()
                self.assertTrue(reader.changed.wait(2))
                reader.worker.join(2)
                reader.changed.clear()
                history = reader.frame[1]
                self.assertEqual(tuple(count for _, count in history), expected)
                self.assertEqual(list(history), sorted(history, key=lambda sample: sample[0]))

    def test_reader_retains_early_samples_for_session_compression(self):
        reader = ConnectionReader()
        with patch('tcp_connections.read_connections', return_value=Connections(7)):
            reader._read()
            first = reader.frame[1][0]
            for _ in range(150):
                reader._read()
        self.assertEqual(len(reader.frame[1]), 151)
        self.assertEqual(reader.frame[1][0], first)
