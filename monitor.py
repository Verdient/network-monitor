#!/usr/bin/env python3
"""Collect one or more interfaces into daily SQLite partitions."""
import argparse
from pathlib import Path
import platform
import subprocess
import time
import math

import storage

def default_interface():
    if platform.system() == 'Darwin':
        for line in subprocess.check_output(['route', '-n', 'get', 'default'], text=True).splitlines():
            if line.strip().startswith('interface:'):
                return line.split(':', 1)[1].strip()
    else:
        for line in subprocess.check_output(['ip', 'route', 'show', 'default'], text=True).splitlines():
            parts = line.split()
            if 'dev' in parts:
                return parts[parts.index('dev') + 1]
    raise RuntimeError('找不到默认网卡，请通过 --interface 指定')

def counters(interface):
    if platform.system() == 'Linux':
        base = '/sys/class/net/' + interface + '/statistics/'
        with open(base + 'rx_bytes') as f:
            rx = int(f.read())
        with open(base + 'tx_bytes') as f:
            tx = int(f.read())
        return rx, tx
    output = subprocess.check_output(['netstat', '-ibn'], text=True)
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 10 and parts[0].rstrip('*') == interface and parts[2].startswith('<Link#'):
            # Read from the right: the optional hardware address may be absent.
            return int(parts[-5]), int(parts[-2])
    raise RuntimeError('找不到网卡: ' + interface)

def interface_names(value):
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    names = [part.strip() for item in values for part in item.split(',')]
    if any(not name for name in names):
        raise ValueError('网卡名称不得为空')
    return list(dict.fromkeys(names))


class InterfacesAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        # Explicit CLI interfaces replace configured defaults; repeated flags add.
        previous = getattr(namespace, self.dest) if getattr(namespace, '_interfaces_explicit', False) else []
        setattr(namespace, self.dest, previous + values)
        namespace._interfaces_explicit = True


def configure_parser(parser, service=False):
    group = parser.add_argument_group('采集设置')
    group.add_argument('-i', '--interface', metavar='NAME', nargs='+', action=InterfacesAction,
                       help='采集网卡，可列出多个、逗号分隔或重复指定；默认使用默认路由网卡')
    group.add_argument('-d', '--database', metavar='DB', default=storage.DEFAULT_DATABASE, help='SQLite 数据库路径')
    group.add_argument('--interval', metavar='SECONDS', type=float, default=1, help='采样间隔，单位秒，最小 1 秒')
    if service:
        parser.set_defaults(count=0)
    else:
        group.add_argument('--count', metavar='N', type=int, default=0, help='采样轮数，0 表示持续运行；仅命令行可指定')


def validate(args, parser):
    if not math.isfinite(args.interval) or args.interval < 1 or args.count < 0:
        parser.error('interval 必须至少为 1 秒，count 不得小于 0')
    try:
        args.interface = interface_names(args.interface)
    except ValueError as error:
        parser.error(str(error))


def run(args, parser):
    validate(args, parser)
    interfaces = args.interface or [default_interface()]
    database = Path(args.database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(str(database) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('该数据库已有采集进程正在使用')
        previous = {interface: counters(interface) for interface in interfaces}
        writer = storage.Writer(database)
        start = time.monotonic()
        n = 0
        print('采集网卡 {}，每 {} 秒采样，按网卡和 Unix 日分表\n数据库：{}'.format(
            ', '.join(interfaces), args.interval, database), flush=True)
        try:
            while args.count == 0 or n < args.count:
                time.sleep(max(0, start + args.interval - time.monotonic()))
                current = {interface: counters(interface) for interface in interfaces}
                end = time.monotonic()
                duration = end - start
                samples = {}
                for interface in interfaces:
                    rx, tx = current[interface]
                    old_rx, old_tx = previous[interface]
                    if rx < old_rx or tx < old_tx:
                        print('网卡 {} 计数器重置，跳过本次采样'.format(interface), flush=True)
                    else:
                        samples[interface] = (tx - old_tx, rx - old_rx)
                writer.write(int(time.time()), duration, samples)
                n += 1
                previous, start = current, end
        except KeyboardInterrupt:
            print('采集已停止', flush=True)
        finally:
            writer.close()


def main(argv=None):
    from network_monitor import main as cli_main
    import sys
    return cli_main(['monitor'] + (sys.argv[1:] if argv is None else list(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
