"""Platform conventions shared by installation and configuration discovery."""

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import sys


RUNTIME_DIRECTORY = 'network-monitor-runtime'


def bundled_runtime(executable):
    extracted = getattr(sys, '_MEIPASS', None)
    if platform.system() != 'Darwin' or extracted is None:
        return None
    runtime = executable.resolve().parent / RUNTIME_DIRECTORY
    if runtime.is_dir() and runtime == Path(extracted).resolve():
        return runtime
    return None


def homebrew_prefix():
    if not getattr(sys, 'frozen', False):
        return None
    # The formula installs this marker beside the real executable in libexec.
    # It works through brew's symlinks and for custom Homebrew prefixes.
    marker = Path(sys.executable).resolve().parent / 'homebrew-prefix'
    if not marker.is_file():
        return None
    prefix = Path(marker.read_text(encoding='utf-8').strip())
    if not prefix.is_absolute():
        raise ValueError('Homebrew 安装标记中的 prefix 必须为绝对路径')
    return prefix


def debian_package():
    if not getattr(sys, 'frozen', False) or platform.system() != 'Linux':
        return False
    return (Path(sys.executable).resolve().parent / 'debian-package').is_file()


@dataclass(frozen=True)
class Layout:
    executable: Path
    config: Path
    data: Path
    services: Path


def layout(user=False):
    system = platform.system()
    home = Path.home()
    prefix = homebrew_prefix() if not user else None
    if prefix is not None:
        return Layout(prefix / 'bin/network-monitor', prefix / 'etc/network-monitor/config.ini',
                      prefix / 'var/lib/network-monitor',
                      home / 'Library/LaunchAgents' if system == 'Darwin' else home / '.config/systemd/user')
    if system == 'Darwin':
        prefix = Path('/opt/homebrew' if platform.machine().lower() in ('arm64', 'aarch64') else '/usr/local')
        data = (home / 'Library/Application Support/network-monitor' if user
                else Path('/Library/Application Support/network-monitor'))
        return Layout(
            (home / '.local/bin' if user else prefix / 'bin') / 'network-monitor',
            data / 'config.ini' if user else prefix / 'etc/network-monitor/config.ini',
            data,
            home / 'Library/LaunchAgents' if user else Path('/Library/LaunchDaemons'))
    if system == 'Linux':
        config_home = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config').expanduser().resolve()
        state_home = Path(os.environ.get('XDG_STATE_HOME') or home / '.local/state').expanduser().resolve()
        return Layout(
            (home / '.local/bin' if user else Path('/usr/local/bin')) / 'network-monitor',
            (config_home / 'network-monitor' if user else Path('/etc/network-monitor')) / 'config.ini',
            state_home / 'network-monitor' if user else Path('/var/lib/network-monitor'),
            config_home / 'systemd/user' if user else Path('/etc/systemd/system'))
    raise ValueError('仅支持 Linux 和 macOS')


def in_path(executable):
    directory = executable.absolute().parent.resolve()
    return any(Path(entry or '.').expanduser().resolve() == directory
               for entry in os.environ.get('PATH', os.defpath).split(os.pathsep))
