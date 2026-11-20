"""Shared command-line presentation."""

import argparse
import os
import re
import shutil
import sys
import configuration


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, indent_increment=2, max_help_position=30,
                         width=min(100, max(60, shutil.get_terminal_size((100, 24)).columns)))

    def _get_help_string(self, action):
        text = action.help or ''
        if (action.option_strings and action.default not in (None, argparse.SUPPRESS)
                and not isinstance(action.default, bool) and '%(default)' not in text):
            text += '（内置默认：%(default)s）'
        return text


class CommandParser(argparse.ArgumentParser):
    def print_help(self, file=None):
        output = sys.stdout if file is None else file
        if not output.isatty() or os.environ.get('TERM') == 'dumb':
            return super().print_help(output)
        color = 'NO_COLOR' not in os.environ

        def accent(label, code):
            return '\033[{}m{}\033[0m'.format(code, label) if color else label

        lines = [accent(' NETWORK ', '1;30;46') + accent(' MONITOR ', '1;30;45')
                 + accent('╲╲', '1;35'), '']
        for line in self.format_help().splitlines():
            if line and not line[0].isspace() and line.endswith((':', '：')):
                line = accent('╸ ' + line, '1;36')
            elif line.lstrip().startswith('-'):
                line = re.sub(r'^(\s+)(-\S.*?)(?=  |$)', lambda m: m[1] + accent(m[2], '1;35'), line)
            lines.append(line)
        print('\n'.join(lines), file=output)


def make_parser(*args, **kwargs):
    parser = CommandParser(*args, formatter_class=HelpFormatter,
                           add_help=False, allow_abbrev=False, **kwargs)
    parser._positionals.title = '位置参数'
    parser._optionals.title = '通用选项'
    parser.add_argument('-h', '--help', action='help', help='显示帮助并退出')
    return parser


def add_config_argument(parser, child=False):
    parser.add_argument('-c', '--config', metavar='PATH',
                        default=argparse.SUPPRESS if child else None,
                        help='指定 INI 配置文件；省略时自动查找')


def config_help():
    user, system = configuration.search_paths()
    return '''参数优先级：命令行 > 配置文件 > 内置默认值。
配置仅支持长期设置：[monitor] interface/database/interval；[stats] timezone。
自动配置：{user}
       → {system}
仅加载第一个存在的文件；使用 -c PATH 可指定其他文件。
count、start/end、图表及安装选项仅支持命令行。'''.format(user=user, system=system)
