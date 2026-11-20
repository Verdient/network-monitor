#!/usr/bin/env python3
"""在临时 CI runner 上验证 deb 安装、升级和卸载的完整生命周期。"""

import argparse
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import time


def run(*command, check=True):
    result = subprocess.run(command, text=True, capture_output=True,
                            env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
    if result.returncode and check:
        raise RuntimeError(shlex.join(command) + '\n' + result.stdout + result.stderr)
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('previous_package', type=Path)
    args = parser.parse_args()
    require(os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true',
            '此脚本只用于临时 GitHub Actions runner，需要 root')
    require(not Path('/etc/network-monitor/config.ini').exists(), '检测到已有配置，拒绝覆盖')
    package = args.package.resolve()
    previous_package = args.previous_package.resolve()
    config = Path('/etc/network-monitor/config.ini')
    database = Path('/var/lib/network-monitor/network-monitor.db')
    runtime = Path('/usr/lib/network-monitor/network-monitor-runtime')
    installed_version = run('dpkg-deb', '-f', str(package), 'Version').stdout.strip()
    run('apt-get', 'install', '-y', str(previous_package))
    try:
        require(run('systemctl', 'is-enabled', 'network-monitor').stdout.strip() == 'enabled', '安装后未启用服务')
        require(run('systemctl', 'show', 'network-monitor', '-p', 'User', '--value').stdout.strip() == 'network-monitor',
                '服务未使用专用用户')
        text = config.read_text().replace('[monitor]\n', '[monitor]\ninterface = lo\n') + '\n# 升级保留配置标记\n'
        config.write_text(text)
        run('systemctl', 'restart', 'network-monitor')
        for _ in range(30):
            result = run('runuser', '-u', 'nobody', '--', 'network-monitor', 'stats', '-i', 'lo',
                         '--no-chart', check=False)
            if result.returncode == 0 and re.search(r'采样：[1-9][\d,]* 次', result.stdout):
                break
            time.sleep(1)
        else:
            raise RuntimeError('普通用户无法查询正在采集的数据库: ' + result.stdout + result.stderr)
        print('安装、自动启动和普通用户查询通过', flush=True)

        def count():
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'samples_%'")
                return sum(connection.execute('SELECT count(*) FROM "{}"'.format(row[0])).fetchone()[0]
                           for row in tables.fetchall())

        before = count()
        run('apt-get', 'install', '-y', str(package))
        version = run('dpkg-query', '-W', '-f=${Version}', 'network-monitor').stdout
        require(version == installed_version, '未升级到预期版本')
        require(config.read_text() == text, '升级覆盖了用户配置')
        require(count() >= before, '升级丢失了历史数据')
        run('systemctl', 'is-active', 'network-monitor')
        for _ in range(15):
            if count() > before:
                break
            time.sleep(1)
        else:
            raise RuntimeError('升级后服务未持续写入')
        pid = run('systemctl', 'show', 'network-monitor', '-p', 'MainPID', '--value').stdout.strip()
        maps = Path('/proc', pid, 'maps').read_text()
        require(str(runtime / 'libpython') in maps, '服务未从安装目录加载 Python 运行时')
        print('从旧版升级、保留配置和历史数据、直接加载运行时均通过', flush=True)

        run('systemctl', 'disable', '--now', 'network-monitor')
        run('apt-get', 'install', '--reinstall', '-y', str(package))
        require(run('systemctl', 'is-enabled', 'network-monitor', check=False).returncode != 0,
                '重新安装启用了用户禁用的服务')
        require(run('systemctl', 'is-active', 'network-monitor', check=False).returncode != 0,
                '重新安装启动了用户停止的服务')
        stopped = run('runuser', '-u', 'nobody', '--', 'network-monitor', 'stats', '-i', 'lo', '--no-chart')
        require('采样：' in stopped.stdout, '普通用户无法查询停止采集后的数据库')
        print('保留服务禁用状态和停止后的普通用户查询通过', flush=True)

        run('apt-get', 'remove', '-y', 'network-monitor')
        require(config.exists() and database.exists(), '卸载删除了配置或历史数据')
        require(not Path('/usr/bin/network-monitor').exists(), '卸载后仍残留可执行文件')
        require(not runtime.exists(), '卸载后仍残留 Python 运行时')
        require(run('systemctl', 'is-active', 'network-monitor', check=False).returncode != 0,
                '卸载后服务仍在运行')
        run('apt-get', 'purge', '-y', 'network-monitor')
        require(not config.exists(), '彻底卸载后配置未移除')
        require(database.exists(), '彻底卸载删除了历史数据')
        print('卸载停止服务、保留数据以及 purge 清理配置通过', flush=True)
    except BaseException:
        result = run('journalctl', '-u', 'network-monitor', '--no-pager', '-n', '100', check=False)
        print(result.stdout, flush=True)
        raise


if __name__ == '__main__':
    main()
