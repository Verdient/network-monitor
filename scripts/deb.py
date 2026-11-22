"""将 Linux 独立可执行文件打包为 Debian 安装包。"""

from email.utils import formatdate
import gzip
import hashlib
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile

from scripts.release import ROOT, check_version, third_party_notices
from install_paths import RUNTIME_DIRECTORY
from shell_completion import SCRIPTS


ARCHITECTURES = {'x86_64': ('amd64', 62), 'aarch64': ('arm64', 183), 'arm64': ('arm64', 183)}


def build(binary, output, version):
    check_version(version)
    if platform.system() != 'Linux':
        raise ValueError('deb 安装包必须在 Linux 上构建')
    architecture, machine = ARCHITECTURES.get(platform.machine().lower(), (None, None))
    if architecture is None:
        raise ValueError('deb 安装包仅支持 amd64 和 arm64')
    if not binary.is_file() or not binary.stat().st_mode & 0o111:
        raise ValueError('找不到可执行的二进制文件: ' + str(binary))
    with binary.open('rb') as file:
        header = file.read(20)
    if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or struct.unpack('<H', header[18:20])[0] != machine:
        raise ValueError('二进制文件必须是与构建机器架构一致的 64 位 Linux ELF 文件')
    runtime = binary.parent / RUNTIME_DIRECTORY
    if not runtime.is_dir():
        raise ValueError('找不到 Linux 运行时目录: ' + str(runtime))
    libc, libc_version = platform.libc_ver()
    if libc != 'glibc' or not libc_version:
        raise ValueError('deb 安装包需要使用 glibc 的构建环境')
    if not shutil.which('dpkg-deb'):
        raise ValueError('构建 deb 安装包需要 dpkg-deb')
    notices = third_party_notices()
    output.mkdir(parents=True, exist_ok=True)
    destination = output.resolve() / 'network-monitor_{}-1_{}.deb'.format(version, architecture)
    with tempfile.TemporaryDirectory(prefix='network-monitor-deb-') as directory:
        package = Path(directory)

        def write(name, content, mode=0o644):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            path.chmod(mode)

        program = package / 'usr/lib/network-monitor/network-monitor'
        program.parent.mkdir(parents=True)
        shutil.copyfile(binary, program)
        program.chmod(0o755)
        shutil.copytree(runtime, program.parent / RUNTIME_DIRECTORY, symlinks=True)
        write('usr/lib/network-monitor/debian-package', 'network-monitor\n')
        (package / 'usr/bin').mkdir(parents=True)
        (package / 'usr/bin/network-monitor').symlink_to('../lib/network-monitor/network-monitor')
        write('usr/share/bash-completion/completions/network-monitor', SCRIPTS['bash'])
        write('usr/share/zsh/vendor-completions/_network_monitor', SCRIPTS['zsh'])
        write('usr/share/fish/vendor_completions.d/network-monitor.fish', SCRIPTS['fish'])
        source = ROOT / 'packaging/debian'
        write('etc/network-monitor/config.ini', (source / 'config.ini').read_text(encoding='utf-8'))
        write('usr/lib/systemd/system/network-monitor.service',
              (source / 'network-monitor.service').read_text(encoding='utf-8'))
        for name in ('postinst', 'prerm', 'postrm'):
            write('DEBIAN/' + name, (source / name).read_text(encoding='utf-8'), 0o755)
        write('DEBIAN/conffiles', '/etc/network-monitor/config.ini\n')
        for name in ('README.md', 'network-monitor.example.ini'):
            write('usr/share/doc/network-monitor/' + name, (ROOT / name).read_text(encoding='utf-8'))
        write('usr/share/doc/network-monitor/copyright', (ROOT / 'LICENSE').read_text(encoding='utf-8')
              + '\n\n' + notices)
        changelog = ('network-monitor ({}-1) stable; urgency=medium\n\n'
                     '  * Debian 安装包，包含独立可执行文件和 systemd 服务。\n\n'
                     ' -- Verdient <22468867+Verdient@users.noreply.github.com>  {}\n').format(
                         version, formatdate(localtime=False, usegmt=True))
        changelog_path = package / 'usr/share/doc/network-monitor/changelog.Debian.gz'
        changelog_path.write_bytes(gzip.compress(changelog.encode('utf-8'), mtime=0))
        files = sorted(path for path in package.rglob('*')
                       if path.is_file() and not path.is_symlink() and 'DEBIAN' not in path.parts)
        size = sum(path.stat().st_size for path in files)
        write('DEBIAN/control', '''Package: network-monitor
Version: {version}-1
Section: net
Priority: optional
Architecture: {architecture}
Maintainer: Verdient <22468867+Verdient@users.noreply.github.com>
Installed-Size: {size}
Depends: libc6 (>= {libc_version}), zlib1g, iproute2, adduser, init-system-helpers (>= 1.54~), tzdata
Recommends: systemd
Homepage: https://github.com/Verdient/network-monitor
Description: Network traffic monitoring with local SQLite history
 Collect multiple network interfaces and query traffic statistics.
 Includes a standalone executable and a systemd service.
'''.format(version=version, architecture=architecture, size=(size + 1023) // 1024, libc_version=libc_version))
        write('DEBIAN/md5sums', ''.join('{}  {}\n'.format(hashlib.md5(path.read_bytes()).hexdigest(),
                                                        path.relative_to(package)) for path in files))
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', '-Zxz', str(package), str(destination)],
                       check=True)
    print(destination)
    return destination
