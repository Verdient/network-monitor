import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tcp_connections import Connections, ConnectionReader, linux_sockets, read_connections


class SocketTableTests(unittest.TestCase):
    def test_linux_combines_both_families_and_groups_ports_by_established_peer(self):
        def encode_words(address):
            return ''.join(int.from_bytes(address[i:i + 4], sys.byteorder).to_bytes(4, 'big').hex()
                           for i in range(0, len(address), 4))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ipv4 = bytes((192, 0, 2, 4))
            remote = encode_words(ipv4)
            (root / 'tcp').write_text('sl local_address rem_address st\n' + '\n'.join(
                '0: ignored {}:0001 {}'.format(remote, state) for state in ('01', '0A', '06', '03')))
            mapped = encode_words(bytes(10) + bytes.fromhex('ffff') + ipv4)
            (root / 'tcp6').write_text('sl local_address remote_address st\n0: ignored ' + mapped + ':0002 01\n')
            with patch('tcp_connections.sys.platform', 'linux'), patch('tcp_connections.linux_sockets',
                                                                       return_value=linux_sockets(root)):
                self.assertEqual(read_connections(), Connections(2, 1, 1, 1, (('192.0.2.4', 2),)))
            (root / 'tcp6').unlink()
            self.assertEqual(len(list(linux_sockets(root))), 4)

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
