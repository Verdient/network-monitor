"""Read local TCP socket tables and estimate outbound ephemeral port headroom."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


Endpoint = tuple[str, int]
Namespace = tuple[int, int] | None
WILDCARDS = {'0.0.0.0', '::', '*'}
LINUX_STATES = {'01': 'ESTABLISHED', '02': 'SYN_SENT', '03': 'SYN_RECV',
                '06': 'TIME_WAIT', '07': 'CLOSED', '0A': 'LISTEN', '0C': 'SYN_RECV'}


@dataclass(frozen=True, slots=True)
class TCPSocket:
    local: Endpoint
    remote: Endpoint
    state: str
    family: int
    namespace: Namespace = None


@dataclass(frozen=True, slots=True)
class PortUsage:
    used: int
    total: int

    @property
    def remaining(self):
        return self.total - self.used

    @property
    def fraction(self):
        return self.used / self.total if self.total else None


@dataclass(frozen=True, slots=True)
class OutboundTarget:
    endpoint: Endpoint
    established: int
    time_wait: int
    usage: PortUsage | None


@dataclass(frozen=True, slots=True)
class LocalConnections:
    address: str
    namespace: Namespace
    outgoing: int
    incoming: int
    incoming_peers: int
    unknown: int
    ports: PortUsage | None
    new_target_remaining: int | None
    targets: tuple[OutboundTarget, ...]


@dataclass(frozen=True)
class Connections:
    established: int = 0
    listening: int = 0
    time_wait: int = 0
    other: int = 0
    peers: tuple = ()
    status: str = ''
    partial: bool = False
    local: tuple[LocalConnections, ...] = ()
    interface: str | None = None
    address_status: str = ''


def normalized_ip(address):
    return str(getattr(address, 'ipv4_mapped', None) or address)


def linux_endpoint(field):
    addr, port = field.split(':')
    # procfs encodes each 32-bit address word in the host byte order.
    packed = b''.join(int(addr[i:i + 8], 16).to_bytes(4, sys.byteorder)
                      for i in range(0, len(addr), 8))
    return normalized_ip(ip_address(packed)), int(port, 16)


def linux_tcp_table(source, family, namespace):
    if 'local_address' not in source.readline():
        raise ValueError('无效的 TCP 表')
    for row in source:
        _, local, remote, state, *_ = row.split()
        yield TCPSocket(linux_endpoint(local), linux_endpoint(remote),
                        LINUX_STATES.get(state, 'OTHER'), family, namespace)


def linux_sockets(proc=Path('/proc')):
    local = proc / 'net'
    directories = [local]
    partial = False
    try:
        with os.scandir(proc) as entries:
            directories.extend(proc / entry.name / 'net' for entry in entries if entry.name.isdecimal())
    except OSError:
        partial = True

    sockets = []
    namespaces = {}
    for directory in directories:
        try:
            table = (directory / 'tcp').stat()
            key = table.st_dev, table.st_ino
            if key not in namespaces:
                with (directory / 'tcp').open(encoding='ascii') as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev, opened.st_ino) != key:
                        continue
                    namespace = None if directory == local else key
                    sockets.extend(list(linux_tcp_table(source, 4, namespace)))
                namespaces[key] = namespace, False
            namespace, has_ipv6 = namespaces[key]
            if not has_ipv6:
                # A different process in the same namespace may expose its IPv6 table.
                with (directory / 'tcp6').open(encoding='ascii') as source:
                    table = (directory / 'tcp').stat()
                    if (table.st_dev, table.st_ino) != key:
                        continue
                    sockets.extend(list(linux_tcp_table(source, 6, namespace)))
                namespaces[key] = namespace, True
        except (FileNotFoundError, ProcessLookupError):
            if directory == local and not namespaces:
                raise
        except (OSError, ValueError):
            if directory == local:
                raise
            partial = True
    return sockets, partial


def darwin_endpoint(field, family):
    address, port = field.rsplit('.', 1)
    if address == '*':
        address = {4: '0.0.0.0', 6: '::'}.get(family, '*')
    else:
        address = normalized_ip(ip_address(address))
    return address, 0 if port == '*' else int(port)


def darwin_sockets(output):
    if not output.strip():
        return
    if 'Local Address' not in output or 'Foreign Address' not in output:
        raise ValueError('无效的 TCP 表')
    families = {'tcp4': 4, 'tcp6': 6, 'tcp46': 46, 'tcp': 0}
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] in families:
            family = families[fields[0]]
            yield TCPSocket(darwin_endpoint(fields[3], family), darwin_endpoint(fields[4], family),
                            fields[5], family)


@lru_cache(maxsize=4)
def port_range(first, last, reserved=''):
    if not 1 <= first <= last <= 65535:
        raise ValueError('无效的临时端口范围')
    ports = set(range(first, last + 1))
    for item in reserved.split(',') if reserved.strip() else ():
        edges = item.split('-')
        start, end = int(edges[0]), int(edges[-1])
        if len(edges) > 2 or not 1 <= start <= end <= 65535:
            raise ValueError('无效的保留端口范围')
        ports.difference_update(range(max(first, start), min(last, end) + 1))
    return frozenset(ports)


def ephemeral_ports():
    try:
        if sys.platform == 'darwin':
            output = subprocess.check_output(
                ['/usr/sbin/sysctl', '-n', 'net.inet.ip.portrange.first', 'net.inet.ip.portrange.last'],
                text=True, stderr=subprocess.DEVNULL, timeout=2)
            return port_range(*map(int, output.split()))
        ipv4 = Path('/proc/sys/net/ipv4')
        first, last = map(int, (ipv4 / 'ip_local_port_range').read_text().split())
        reserved = (ipv4 / 'ip_local_reserved_ports').read_text()
        return port_range(first, last, reserved)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def endpoint_ports(listeners, address, dual_stack=False):
    wildcard = ('::', '*') if ':' in address else ('0.0.0.0', '*')
    if dual_stack and ':' not in address:
        wildcard += ('::',)
    ports = set()
    for host in (address, *wildcard):
        ports.update(listeners.get(host, ()))
    return ports


def socket_direction(sock, hints, listen_ports, ports):
    key = (sock.namespace, sock.local, sock.remote)
    direction = hints.get(key)
    if sock.state == 'SYN_SENT':
        direction = 'out'
    elif sock.state in ('SYN_RECV', 'SYN_RECEIVED'):
        direction = 'in'
    if direction is not None:
        return direction, key
    if sock.local[1] in listen_ports:
        return 'in', None
    if ports is not None and sock.local[1] in ports:
        return 'out', None
    return 'unknown', None


def summarize_local(address, namespace, sockets, listeners, ports, hints, next_hints):
    listen_ports = endpoint_ports(listeners, address)
    dual_stack_ports = endpoint_ports(listeners, address, dual_stack=True)
    # procfs cannot distinguish dual-stack IPv6 wildcard listeners from IPv6-only
    # listeners. Account for the possible conflict in the estimate.
    possible_conflicts = dual_stack_ports if sys.platform.startswith('linux') else listen_ports
    # Keep listeners in the denominator and account for them once in occupancy.
    blocked = possible_conflicts & ports if ports is not None else set()
    occupied = set(blocked)
    by_target = defaultdict(set)
    outbound = set()
    established, established_out, wait = Counter(), Counter(), Counter()
    incoming_peers = set()
    for sock in sockets:
        port = sock.local[1]
        target = sock.remote
        direction, key = socket_direction(sock, hints, dual_stack_ports if sock.family == 6 else listen_ports, ports)
        if key is not None:
            next_hints[key] = direction
        if sock.state == 'ESTABLISHED':
            established[direction] += 1
            if direction == 'in':
                incoming_peers.add(target[0])
            elif direction == 'out':
                established_out[target] += 1
        if direction == 'out' and target[1]:
            outbound.add(target)
        if ports is not None and port in ports:
            occupied.add(port)
            if port not in blocked:
                by_target[target].add(port)
            if sock.state == 'TIME_WAIT':
                wait[target] += 1
        elif ports is None and sock.state == 'TIME_WAIT':
            wait[target] += 1

    total = len(ports) if ports is not None else None
    targets = [OutboundTarget(target, established_out[target], wait[target],
                              PortUsage(len(blocked) + len(by_target.get(target, ())), total)
                              if total is not None else None)
               for target in outbound]
    targets.sort(key=lambda target: (-(target.usage.used if target.usage else 0), -target.established,
                                     target.endpoint))
    return LocalConnections(address, namespace, established['out'], established['in'], len(incoming_peers),
                            established['unknown'], PortUsage(len(occupied), total) if total is not None else None,
                            total - len(blocked) if total is not None else None, tuple(targets))


def local_connections(sockets, interface, ports, hints):
    from ip_traffic import interface_addresses

    try:
        addresses, _ = interface_addresses(interface)
        selected = {normalized_ip(ip_address(address)) for address in addresses}
        address_status = '' if selected else '网卡暂无 IP'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        selected = set()
        address_status = '网卡 IP 不可用'

    listeners = defaultdict(lambda: defaultdict(set))
    connections = defaultdict(list)
    for sock in sockets:
        address = sock.local[0]
        host, _, scope = address.partition('%')
        if sock.namespace is None and scope and scope != interface:
            continue
        if sock.state == 'LISTEN':
            listeners[sock.namespace][host].add(sock.local[1])
            continue
        if sock.state == 'CLOSED' or address in WILDCARDS:
            continue
        if sock.namespace is not None or host in selected:
            connections[sock.namespace, host if sock.namespace is None else address].append(sock)
    for address in selected:
        connections.setdefault((None, address), [])

    next_hints = {}
    groups = [
        summarize_local(address, namespace, group, listeners[namespace], ports if namespace is None else None,
                        hints, next_hints)
        for (namespace, address), group in connections.items()
    ]
    groups.sort(key=lambda group: (
        group.namespace is not None,
        -(group.targets[0].usage.used if group.targets and group.targets[0].usage else 0),
        -group.outgoing, group.address, group.namespace or (0, 0),
    ))
    return tuple(groups), address_status, next_hints


def read_connections(interface=None, hints=None):
    try:
        partial = False
        if sys.platform == 'darwin':
            output = subprocess.check_output(
                ['/usr/sbin/netstat', '-anW', '-p', 'tcp'], text=True, stderr=subprocess.DEVNULL,
                env=dict(os.environ, LC_ALL='C'), timeout=3)
            sockets = list(darwin_sockets(output))
        elif sys.platform.startswith('linux'):
            sockets, partial = linux_sockets()
        else:
            return Connections(status='系统不支持')
        states = Counter(sock.state for sock in sockets)
        peers = Counter(sock.remote[0] for sock in sockets if sock.state == 'ESTABLISHED')
        est, listen, wait = (states.pop(s, 0) for s in ('ESTABLISHED', 'LISTEN', 'TIME_WAIT'))
        ranked = sorted(peers.items(), key=lambda item: (-item[1], item[0]))
        groups, status = (), ''
        if interface is not None:
            groups, status, next_hints = local_connections(sockets, interface, ephemeral_ports(), hints or {})
            if hints is not None:
                hints.clear()
                hints.update(next_hints)
        return Connections(est, listen, wait, sum(states.values()), tuple(ranked), partial=partial,
                           local=groups, interface=interface, address_status=status)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        if hints is not None:
            hints.clear()
        return Connections(status='连接表不可用')


class ConnectionReader:
    """Publish a complete immutable frame. Slow socket reads never hold up the terminal."""
    def __init__(self, interface=None):
        self.interface = interface
        self.hints = {}
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
        snapshot = read_connections(self.interface, self.hints)
        timestamp = self.wall_time_offset + time.monotonic()
        self.history.append((timestamp, None if snapshot.status else snapshot.established))
        self.frame = snapshot, tuple(self.history)
        self.changed.set()
