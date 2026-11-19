"""Install a persistent monitor using systemd or launchd."""

import configparser
from dataclasses import dataclass
import io
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

import monitor
import install_paths


LABEL = 'io.network-monitor'
UNIT = 'network-monitor.service'
SOURCE_FILES = ('network_monitor.py', 'monitor.py', 'stats.py', 'configuration.py',
                'cli_support.py', 'service_install.py', 'storage.py', 'time_ranges.py', 'install_paths.py', 'version.py')


def configure_parser(parser):
    monitor.configure_parser(parser, service=True)
    group = parser.add_argument_group('服务安装')
    scope = group.add_mutually_exclusive_group()
    scope.add_argument('--user', action='store_true', default=False, help='安装当前用户服务，无需 sudo；登录后启动')
    scope.add_argument('--system', dest='user', action='store_false', default=False, help='安装系统服务，开机启动，需要 root（默认）')
    group.add_argument('--work-dir', metavar='DIR', help='数据目录；默认使用系统或用户数据目录')
    group.add_argument('--dry-run', action='store_true', help='预览路径、配置与服务定义，不写入或启动服务')
    group.add_argument('-y', '--yes', action='store_true', help='自动同意将程序和配置迁移到默认位置，适合非交互安装')


def systemd_quote(value, command=False):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    value = value.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


@dataclass
class InstallPlan:
    backend: str
    user: bool
    executable: Path
    config: Path
    work_dir: Path
    service: Path
    database: Path
    config_text: str
    service_content: bytes
    domain: str = ''
    source: Path = None
    config_source: Path = None
    move_executable: bool = False
    move_config: bool = False


def make_plan(args, parser, executable=None, config=None):
    monitor.validate(args, parser)
    system = platform.system()
    if system not in ('Linux', 'Darwin'):
        raise RuntimeError('install 仅支持 Linux systemd 和 macOS launchd')
    if not shutil.which('systemctl' if system == 'Linux' else 'launchctl'):
        raise RuntimeError('当前系统缺少 {}'.format('systemctl（需要 systemd）' if system == 'Linux' else 'launchctl'))
    locations = install_paths.layout(user=args.user)
    source = Path(sys.executable).absolute() if getattr(sys, 'frozen', False) else None
    if executable is None:
        executable = source if source and install_paths.in_path(source) else locations.executable
    config = config if config is not None else locations.config
    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else locations.data.resolve()
    if '\n' in str(work_dir) or '\r' in str(work_dir) or str(work_dir).rstrip() != str(work_dir):
        raise RuntimeError('服务数据目录不能包含换行符或以空白结尾')
    database = (work_dir / Path(args.database).expanduser()).resolve()
    # Save the effective values so the service never depends on the invoking cwd
    # or on an optional configuration file that might later be moved.
    saved = configparser.ConfigParser(interpolation=None)
    saved['monitor'] = {'database': str(database), 'interval': str(args.interval)}
    if args.interface:
        saved['monitor']['interface'] = ', '.join(args.interface)
    stats_defaults = dict(args.config_defaults.get('stats', {}))
    if stats_defaults:
        saved['stats'] = {key: str(value) for key, value in stats_defaults.items()}
    text = io.StringIO()
    saved.write(text)
    command = [str(executable), '--config', str(config), 'monitor']
    domain = ''
    if system == 'Linux':
        service = locations.services / UNIT
        # WorkingDirectory consumes a raw path. ExecStart quotes arguments, but
        # only arguments after the executable expand environment variables.
        service_text = '''[Unit]
Description=Network Monitor
After=network.target

[Service]
Type=simple
ExecStart={command}
WorkingDirectory={work_dir}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy={target}
'''.format(command=' '.join(systemd_quote(part, command=index > 0) for index, part in enumerate(command)),
           work_dir=str(work_dir).replace('%', '%%'), target='default.target' if args.user else 'multi-user.target')
        content = service_text.encode('utf-8')
    else:
        service = locations.services / (LABEL + '.plist')
        domain = 'gui/{}'.format(os.getuid()) if args.user else 'system'
        content = plistlib.dumps({
            'Label': LABEL, 'ProgramArguments': command, 'WorkingDirectory': str(work_dir),
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 5,
            'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': str(work_dir / 'service.log'),
            'StandardErrorPath': str(work_dir / 'service.error.log'),
        })
    return InstallPlan('systemd' if system == 'Linux' else 'launchd', args.user,
                       executable, config, work_dir, service, database, text.getvalue(), content, domain,
                       source=source, config_source=getattr(args, 'config_path', None))


def confirm_move(message, yes=False):
    if yes:
        return True
    while True:
        try:
            answer = input(message + ' [Y/n]: ').strip().lower()
        except EOFError:
            raise RuntimeError('安装需要确认迁移路径；请在终端中回答，或使用 --yes 进行非交互安装') from None
        if answer in ('', 'y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print('请输入 y 或 n；直接回车表示同意。')


def select_paths(args, parser, plan):
    executable, config = plan.executable, plan.config
    move_executable = move_config = False
    if plan.source and plan.source.resolve() != executable.resolve():
        move_executable = confirm_move(
            '当前二进制 {} 不在 PATH 中，是否移动到 {}？'.format(plan.source, executable), args.yes)
        if not move_executable:
            executable = plan.source
    if plan.config_source and plan.config_source.resolve() != config.resolve():
        move_config = confirm_move(
            '配置 {} 不在默认位置，是否迁移到 {}？'.format(plan.config_source, config), args.yes)
        if not move_config:
            config = plan.config_source
    selected = make_plan(args, parser, executable=executable, config=config)
    selected.move_executable, selected.move_config = move_executable, move_config
    return selected


def run_command(command):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('命令执行失败: {}\n{}'.format(
            shlex.join(command), (error.stderr or error.stdout or str(error)).strip())) from error


def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_program(plan):
    if plan.source is not None or getattr(sys, 'frozen', False):
        source = plan.source or Path(sys.executable)
        if source.resolve() != plan.executable.resolve():
            atomic_write(plan.executable, source.read_bytes(), 0o755)
    else:
        app = plan.work_dir / 'app'
        for name in SOURCE_FILES:
            atomic_write(app / name, (Path(__file__).resolve().parent / name).read_bytes(), 0o644)
        launcher = '#!/bin/sh\nexec {} {} "$@"\n'.format(
            shlex.quote(str(Path(sys.executable).resolve())), shlex.quote(str(app / 'network_monitor.py')))
        atomic_write(plan.executable, launcher.encode('utf-8'), 0o755)


def install(plan):
    if not plan.user and os.geteuid() != 0:
        raise RuntimeError('安装系统服务需要 root，请使用 sudo，或改用 install --user')
    manager = ['systemctl'] + (['--user'] if plan.user else [])
    # Check that the target service manager is usable before writing files.
    if plan.backend == 'systemd':
        run_command(manager + ['show-environment'])
    else:
        run_command(['launchctl', 'print', plan.domain])
    plan.work_dir.mkdir(parents=True, exist_ok=True)
    plan.database.parent.mkdir(parents=True, exist_ok=True)
    install_program(plan)
    atomic_write(plan.config, plan.config_text.encode('utf-8'), 0o644)
    atomic_write(plan.service, plan.service_content, 0o644)
    if plan.backend == 'systemd':
        run_command(manager + ['daemon-reload'])
        run_command(manager + ['enable', UNIT])
        run_command(manager + ['restart', UNIT])
    else:
        target = plan.domain + '/' + LABEL
        existing = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True)
        if existing.returncode == 0:
            run_command(['launchctl', 'bootout', target])
        run_command(['launchctl', 'enable', target])
        run_command(['launchctl', 'bootstrap', plan.domain, str(plan.service)])
    # Stage a copy first and only remove originals after the service starts.
    for moving, source in ((plan.move_executable, plan.source), (plan.move_config, plan.config_source)):
        if moving and source is not None:
            try:
                source.unlink()
            except OSError as error:
                raise RuntimeError('服务已启动，但无法移除原文件 {}: {}'.format(source, error)) from error


def run(args, parser):
    if install_paths.homebrew_prefix() is not None:
        raise RuntimeError('通过 Homebrew 安装的程序请使用 brew services start network-monitor 启动，'
                           '使用 brew services restart/stop network-monitor 管理服务')
    plan = make_plan(args, parser)
    if not args.dry_run:
        plan = select_paths(args, parser, plan)
    print('{} · {} · {}'.format('安装预览' if args.dry_run else '安装后台采集服务',
                                plan.backend, '当前用户' if plan.user else '系统服务'))
    print('程序：{}\n配置：{}\n数据：{}\n服务：{}'.format(
        plan.executable, plan.config, plan.work_dir, plan.service))
    if args.dry_run:
        print('迁移提示默认选择 Yes；--yes 可跳过确认。预览按默认选择生成。')
        print('\n配置内容：\n' + plan.config_text)
        print('服务定义：\n' + plan.service_content.decode('utf-8'))
        return
    install(plan)
    print('\n服务已安装并启动。')
    if not install_paths.in_path(plan.executable):
        print('程序目录尚未加入 PATH，可执行：export PATH={}:"$PATH"'.format(shlex.quote(str(plan.executable.parent))))
    if plan.backend == 'systemd':
        command = 'systemctl' + (' --user' if plan.user else '')
        print('查看状态：{} status {}\n查看日志：journalctl {}-u {} -f'.format(
            command, UNIT, '--user ' if plan.user else '', UNIT))
    else:
        print('查看状态：launchctl print {}/{}\n日志目录：{}'.format(plan.domain, LABEL, plan.work_dir))
