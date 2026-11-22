"""Count local IP traffic from short tcpdump snapshots on one interface."""
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
from queue import Full, Queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from ip_location import IPLocations, peer_ip


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


def traffic_rows(totals, delta, factor, addresses=None):
    for address, (down, up) in delta.items():
        counts = totals.setdefault(address, [0, 0])
        counts[0] += down
        counts[1] += up
    rows = []
    visible_addresses = totals if addresses is None else addresses
    for address in visible_addresses:
        total_down, total_up = totals.get(address, (0, 0))
        down, up = delta.get(address, (0, 0))
        rows.append(PeerTraffic(str(ip_address(address)), total_down, total_up, down * factor, up * factor))
    return sorted(rows, key=lambda row: (-(row.down_mbps + row.up_mbps), -(row.download + row.upload), row.address))


class IPTraffic:
    def __init__(self, interface, changed):
        self.interface = interface
        self.changed = changed
        self.process = None
        self.worker = None
        self.lock = threading.Lock()
        self.pending = {}
        self.totals = {}
        self.local_pending = {}
        self.local_totals = {}
        self.local_addresses = set()
        self.broadcasts = set()
        self.previous = None
        self.rows = []
        self.local_rows = []
        self.status = ''
        self.ready = False
        self.hostnames = {}
        self.hostname_requests = Queue(maxsize=32)
        self.hostname_worker = None
        self.closed = threading.Event()
        self.locations = IPLocations(changed)

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
        errors = []
        stop_addresses = threading.Event()
        address_worker = None

        def read_errors():
            for line in proc.stderr:
                message = line.decode(errors='replace').strip()
                if message.startswith('tcpdump: listening on '):
                    self.ready = True
                    self.changed.set()
                elif message:
                    errors.append(message)

        diagnostics = threading.Thread(target=read_errors, daemon=True)
        diagnostics.start()
        try:
            local, broadcasts = interface_addresses(self.interface)
            with self.lock:
                self.local_addresses, self.broadcasts = local, broadcasts
            address_worker = threading.Thread(target=self._refresh_addresses, args=(stop_addresses,), daemon=True)
            address_worker.start()
            for src, dst, length in ip_packets(proc.stdout):
                multicast = dst[0] == 255 if len(dst) == 16 else 224 <= dst[0] <= 239
                with self.lock:
                    send = src in self.local_addresses
                    if dst in self.local_addresses:
                        self.local_pending.setdefault(dst, [0, 0])[0] += length
                        self.pending.setdefault(src, [0, 0])[0] += length
                    elif not send and (multicast or dst in self.broadcasts or dst == b'\xff' * 4):
                        # A received group packet has no individual local destination IP.
                        self.pending.setdefault(src, [0, 0])[0] += length
                    if send:
                        self.local_pending.setdefault(src, [0, 0])[1] += length
                        self.pending.setdefault(dst, [0, 0])[1] += length
            self.status = 'IP 采集已结束'
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.status = '无法采集 IP 流量：' + str(error)
        finally:
            stop_addresses.set()
            if proc.poll() is None:
                proc.terminate()
            proc.stdout.close()
            proc.wait()
            diagnostics.join()
            proc.stderr.close()
            if address_worker is not None:
                address_worker.join()
            if proc.returncode and errors:
                if any('permi' in message.lower() for message in errors):
                    self.status = '当前用户没有此网卡的抓包权限'
                else:
                    self.status = errors[0]
            self.ready = False
            self.changed.set()

    def _refresh_addresses(self, stop):
        while not stop.wait(10):
            try:
                local, broadcasts = interface_addresses(self.interface)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                continue
            with self.lock:
                self.local_addresses, self.broadcasts = local, broadcasts

    def sample(self):
        if self.previous is None:
            return
        with self.lock:
            now = time.monotonic()
            delta, self.pending = self.pending, {}
            local_delta, self.local_pending = self.local_pending, {}
            local = self.local_addresses
        elapsed = now - self.previous
        self.previous = now
        factor = 8 / elapsed / 1e6 if elapsed > 0 else 0
        self.rows = traffic_rows(self.totals, delta, factor)
        self.local_rows = traffic_rows(self.local_totals, local_delta, factor, local)

    def hostname(self, address):
        with self.lock:
            if address not in self.hostnames and not self.closed.is_set():
                if peer_ip(address).is_multicast:
                    self.hostnames[address] = ''
                    return ''
                try:
                    self.hostname_requests.put_nowait(address)
                except Full:
                    return ''
                self.hostnames[address] = ''
                if self.hostname_worker is None:
                    self.hostname_worker = threading.Thread(target=self._resolve_hostnames, daemon=True)
                    self.hostname_worker.start()
            return self.hostnames.get(address, '')

    def _resolve_hostnames(self):
        while not self.closed.is_set():
            address = self.hostname_requests.get()
            if address is None or self.closed.is_set():
                return
            try:
                name = socket.gethostbyaddr(address)[0].rstrip('.')
            except OSError:
                continue
            if name.casefold() == '_gateway':
                name = '默认网关'
            if name and name != address:
                with self.lock:
                    self.hostnames[address] = name
                self.changed.set()

    def peer_details(self, address):
        return self.locations.lookup(address)

    def close(self):
        self.locations.close()
        self.closed.set()
        if self.hostname_worker is not None:
            try:
                self.hostname_requests.put_nowait(None)
            except Full:
                pass
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
