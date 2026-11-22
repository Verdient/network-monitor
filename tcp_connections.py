"""Read local TCP socket tables for the live dashboard."""
from collections import Counter
from dataclasses import dataclass
from ipaddress import ip_address
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


@dataclass(frozen=True)
class Connections:
    established: int = 0
    listening: int = 0
    time_wait: int = 0
    other: int = 0
    peers: tuple = ()
    status: str = ''


def linux_sockets(directory=Path('/proc/net')):
    for name in ('tcp', 'tcp6'):
        try:
            source = (directory / name).open(encoding='ascii')
        except FileNotFoundError:
            if name == 'tcp6':  # IPv6 can be disabled in the kernel.
                continue
            raise
        with source:
            if 'local_address' not in source.readline():
                raise ValueError('无效的 TCP 表')
            states = {'01': 'ESTABLISHED', '0A': 'LISTEN', '06': 'TIME_WAIT'}
            for row in source:
                _, _, remote, state, *_ = row.split()
                peer = None
                if state == '01':
                    addr, _ = remote.split(':')
                    # procfs encodes each 32-bit word in the host byte order.
                    packed = b''.join(int(addr[i:i + 8], 16).to_bytes(4, sys.byteorder)
                                      for i in range(0, len(addr), 8))
                    address = ip_address(packed)
                    peer = str(getattr(address, 'ipv4_mapped', None) or address)
                yield states.get(state, 'OTHER'), peer


def darwin_sockets(output):
    if not output.strip():
        return
    if 'Local Address' not in output or 'Foreign Address' not in output:
        raise ValueError('无效的 TCP 表')
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] in ('tcp4', 'tcp6', 'tcp46', 'tcp'):
            state = fields[5]
            peer = None
            if state == 'ESTABLISHED':
                # Darwin netstat uses a final dot before the numeric port for both families.
                address = ip_address(fields[4].rsplit('.', 1)[0])
                peer = str(getattr(address, 'ipv4_mapped', None) or address)
            yield state, peer


def read_connections():
    try:
        if sys.platform == 'darwin':
            output = subprocess.check_output(
                ['/usr/sbin/netstat', '-anW', '-p', 'tcp'], text=True, stderr=subprocess.DEVNULL,
                env=dict(os.environ, LC_ALL='C'), timeout=3)
            sockets = darwin_sockets(output)
        elif sys.platform.startswith('linux'):
            sockets = linux_sockets()
        else:
            return Connections(status='系统不支持')
        states, peers = Counter(), Counter()
        for state, peer in sockets:
            states[state] += 1
            if peer is not None:
                peers[peer] += 1
        est, listen, wait = (states.pop(s, 0) for s in ('ESTABLISHED', 'LISTEN', 'TIME_WAIT'))
        ranked = sorted(peers.items(), key=lambda item: (-item[1], item[0]))
        return Connections(est, listen, wait, sum(states.values()), tuple(ranked))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return Connections(status='连接表不可用')


class ConnectionReader:
    """Publish a complete immutable frame. Slow socket reads never hold up the terminal."""
    def __init__(self):
        self.worker = None
        self.changed = threading.Event()
        self.history = []
        self.wall_time_offset = time.time() - time.monotonic()
        self.frame = Connections(status='读取中'), ()

    def refresh(self):
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._read, daemon=True)
            self.worker.start()

    def _read(self):
        snapshot = read_connections()
        timestamp = self.wall_time_offset + time.monotonic()
        self.history.append((timestamp, None if snapshot.status else snapshot.established))
        self.frame = snapshot, tuple(self.history)
        self.changed.set()
