#!/usr/bin/env python3
"""网络流量采集与统计的统一命令行入口。"""

from pathlib import Path
import sqlite3
import subprocess
import sys

import monitor
import stats
import service_install
import configuration
from cli_support import config_help, add_config_argument, make_parser
from version import __version__


EXAMPLES = {
    'monitor': '''示例：
  network-monitor monitor                         持续采集默认网卡
  network-monitor monitor -i eth0 --count 60        采集 60 次
  network-monitor monitor -i eth0 wlan0            同时采集两张网卡
  network-monitor monitor -c ./custom.ini          使用指定配置
''',
    'stats': '''示例：
  network-monitor stats                           统计今天的记录
  network-monitor stats -i eth0 --timezone UTC      按 UTC 统计 eth0
  network-monitor stats --start 2026-11-01 --end 2026-11-20
  network-monitor stats --no-chart                 只显示统计表
''',
    'install': '''示例：
  sudo network-monitor install                    安装并启动系统服务
  network-monitor install --user                   安装并启动当前用户服务
  network-monitor install -c custom.ini --dry-run   预览安装内容
''',
}


def build_parser():
    configuration_help = config_help()
    parser = make_parser(prog='network-monitor',
                         description='network-monitor · 网络流量采集与统计',
                         epilog='''快速开始：
  network-monitor monitor            开始采集，Ctrl+C 停止
  network-monitor stats              查看今天的统计与趋势
  network-monitor install --user     安装为当前用户的后台服务

使用 network-monitor <命令> -h 查看详细参数。

''' + configuration_help)
    add_config_argument(parser)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__,
                        help='显示版本号并退出')
    commands = parser.add_subparsers(dest='command', required=True, title='可用命令',
                                     metavar='<命令>', parser_class=make_parser)
    command_parsers = {}
    for name, module, description in (
        ('monitor', monitor, '同时采集多张网卡，保存到 SQLite 日分表'),
        ('stats', stats, '按网卡、时间范围和时区查询统计与趋势'),
        ('install', service_install, '自动识别系统，安装并启动后台采集服务'),
    ):
        command = commands.add_parser(name, help=description, description=description,
                                      epilog=EXAMPLES[name] + '\n' + configuration_help)
        add_config_argument(command, child=True)
        module.configure_parser(command)
        command.set_defaults(handler=module.run, command_parser=command)
        command_parsers[name] = command
    return parser, command_parsers


def main(argv=None):
    parser, command_parsers = build_parser()
    args = parser.parse_args(argv)
    try:
        defaults, config_path = configuration.load(args.config)
    except ValueError as error:
        parser.error(str(error))
    command = command_parsers[args.command]
    if args.command == 'install':
        command.set_defaults(**defaults.get('monitor', {}))
    if args.command == 'stats' and 'database' in defaults.get('monitor', {}):
        command.set_defaults(database=defaults['monitor']['database'])
    command.set_defaults(**defaults.get(args.command, {}))
    args = parser.parse_args(argv)
    args.config_path = config_path
    args.config_defaults = defaults
    args.database = str(Path(args.database).expanduser())
    try:
        args.handler(args, args.command_parser)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('{}: 错误: {}'.format(parser.prog, error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
