"""Count local IP traffic from short tcpdump snapshots on one interface."""
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time


def system_environment():
    env = dict(os.environ, LC_ALL='C')
    if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
        # PyInstaller prepends its private libraries. System tools need their original ABI.
        env.pop('LD_LIBRARY_PATH', None)
        if 'LD_LIBRARY_PATH_ORIG' in env:
            env['LD_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH_ORIG')
    return env


def interface_addresses(interface):
    broadcasts = []
    if sys.platform == 'darwin':
        output = subprocess.check_output(['/sbin/ifconfig', interface], text=True,
                                         stderr=subprocess.DEVNULL, timeout=2)
        addresses = []
        for line in output.splitlines():
            parts = line.split()
            if parts and parts[0] in ('inet', 'inet6'):
                addresses.append(parts[1].split('%')[0])
                if 'broadcast' in parts:
                    broadcasts.append(parts[parts.index('broadcast') + 1])
    else:
        output = subprocess.check_output(['ip', '-j', 'address', 'show', 'dev', interface],
                                         text=True, stderr=subprocess.DEVNULL, timeout=2, env=system_environment())
        entries = [entry for device in json.loads(output) for entry in device['addr_info']]
        addresses = [entry['local'] for entry in entries]
        broadcasts = [entry['broadcast'] for entry in entries if 'broadcast' in entry]
    return {ip_address(a).packed for a in addresses}, {ip_address(a).packed for a in broadcasts}


def ip_packets(stream):
    header = stream.read(24)
    if len(header) != 24:
        raise ValueError('未能读取采集数据')
    endian = {b'\xd4\xc3\xb2\xa1': '<', b'\x4d\x3c\xb2\xa1': '<',
              b'\xa1\xb2\xc3\xd4': '>', b'\xa1\xb2\x3c\x4d': '>'}.get(header[:4])
    if not endian:
        raise ValueError('不支持的采集格式')
    link = struct.unpack_from(endian + 'I', header, 20)[0] & 0xffff
    # Ethernet, NULL/LOOP, raw IP, Linux cooked v1/v2 and explicit IPv4/IPv6.
    offsets = {1: 14, 0: 4, 108: 4, 101: 0, 113: 16, 276: 20, 228: 0, 229: 0}
    if link not in offsets:
        raise ValueError('不支持此网卡的链路类型 {}'.format(link))
    record = struct.Struct(endian + 'IIII')
    while True:
        header = stream.read(record.size)
        if not header:
            return
        if len(header) != record.size:
            raise ValueError('采集记录不完整')
        _, _, captured, _ = record.unpack(header)
        if captured > 96:
            raise ValueError('采集记录长度异常')
        packet = stream.read(captured)
        if len(packet) != captured:
            raise ValueError('采集记录不完整')
        offset = offsets[link]
        if link in (1, 113, 276):
            kind_at = 0 if link == 276 else offset - 2
            kind = packet[kind_at:kind_at + 2]
            while kind in (b'\x81\x00', b'\x88\xa8', b'\x91\x00'):
                kind = packet[offset + 2:offset + 4]
                offset += 4
            if kind not in (b'\x08\x00', b'\x86\xdd'):
                continue
        ip = packet[offset:]
        if len(ip) >= 20 and ip[0] >> 4 == 4:
            length = int.from_bytes(ip[2:4], 'big')
            if length >= (ip[0] & 15) * 4 >= 20:
                yield ip[12:16], ip[16:20], length
        elif len(ip) >= 40 and ip[0] >> 4 == 6:
            yield ip[8:24], ip[24:40], 40 + int.from_bytes(ip[4:6], 'big')


@dataclass(frozen=True)
class PeerTraffic:
    address: str
    download: int
    upload: int
    down_mbps: float
    up_mbps: float


class IPTraffic:
    def __init__(self, interface, changed):
        self.interface = interface
        self.changed = changed
        self.process = None
        self.worker = None
        self.lock = threading.Lock()
        self.pending = {}
        self.totals = {}
        self.previous = None
        self.rows = []
        self.status = ''

    def start(self):
        binary = shutil.which('tcpdump')
        if not binary and sys.platform == 'darwin':
            binary = '/usr/sbin/tcpdump'
        if not binary:
            self.status = '找不到 tcpdump，请先安装 tcpdump'
            return
        try:
            self.process = subprocess.Popen(
                [binary, '-p', '-nn', '--immediate-mode', '-U', '-s', '96', '-i', self.interface, '-w', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=system_environment())
        except OSError as error:
            self.status = '无法启动 tcpdump：' + str(error)
            return
        self.previous = time.monotonic()
        self.worker = threading.Thread(target=self._read, daemon=True)
        self.worker.start()

    def _read(self):
        proc = self.process
        try:
            local, broadcasts = interface_addresses(self.interface)
            refresh_at = time.monotonic() + 10
            for src, dst, length in ip_packets(proc.stdout):
                now = time.monotonic()
                if now >= refresh_at:
                    local, broadcasts = interface_addresses(self.interface)
                    refresh_at = now + 10
                receive = dst in local
                send = src in local
                multicast = dst[0] == 255 if len(dst) == 16 else 224 <= dst[0] <= 239
                if not send and (multicast or dst in broadcasts or dst == b'\xff' * 4):
                    receive = True
                with self.lock:
                    if receive:
                        self.pending.setdefault(src, [0, 0])[0] += length
                    if send:
                        self.pending.setdefault(dst, [0, 0])[1] += length
            self.status = 'IP 采集已结束'
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.status = '无法采集 IP 流量：' + str(error)
        finally:
            if proc.poll() is None:
                proc.terminate()
            _, errors = proc.communicate()
            if proc.returncode and errors:
                messages = errors.decode(errors='replace').strip().splitlines()
                if any('permi' in message.lower() for message in messages):
                    self.status = '当前用户没有此网卡的抓包权限'
                elif messages and not messages[0].startswith('tcpdump: listening on'):
                    self.status = messages[0]
            self.changed.set()

    def sample(self):
        if self.previous is None:
            return
        with self.lock:
            now = time.monotonic()
            delta, self.pending = self.pending, {}
        elapsed = now - self.previous
        self.previous = now
        for peer, (down, up) in delta.items():
            totals = self.totals.setdefault(peer, [0, 0])
            totals[0] += down
            totals[1] += up
        rows = []
        factor = 8 / elapsed / 1e6 if elapsed > 0 else 0
        for peer, (total_down, total_up) in self.totals.items():
            down, up = delta.get(peer, (0, 0))
            rows.append(PeerTraffic(str(ip_address(peer)), total_down, total_up, down * factor, up * factor))
        self.rows = sorted(rows, key=lambda row: (-(row.down_mbps + row.up_mbps),
                                                 -(row.download + row.upload), row.address))

    def close(self):
        proc = self.process
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.worker.join()
