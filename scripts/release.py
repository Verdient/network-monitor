#!/usr/bin/env python3
"""Build release archives and generate a formula from their actual checksums."""

import argparse
import hashlib
from importlib.metadata import distribution
from pathlib import Path
import platform
import re
import subprocess
import sys
import sysconfig
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from version import __version__
from install_paths import RUNTIME_DIRECTORY


def check_version(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or version != __version__:
        raise ValueError('版本必须与 version.py 一致，当前为 ' + __version__)


def third_party_notices():
    python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
    if not python_license.is_file():
        raise ValueError('找不到 Python LICENSE.txt，不能生成缺少依赖许可证的发布包')
    package = distribution('pyinstaller')
    pyinstaller_license = next((Path(package.locate_file(file)) for file in package.files or []
                               if str(file).endswith('/COPYING.txt')), None)
    if pyinstaller_license is None or not pyinstaller_license.is_file():
        raise ValueError('找不到 PyInstaller COPYING.txt')
    import sqlite3
    return ('Python runtime license\n======================\n' + python_license.read_text(encoding='utf-8')
            + '\n\nPyInstaller license and bootloader exception\n===========================================\n'
            + pyinstaller_license.read_text(encoding='utf-8')
            + '\n\nSQLite {}\nSQLite is in the public domain. https://sqlite.org/copyright.html\n'.format(sqlite3.sqlite_version))


def archive(binary, output, version):
    check_version(version)
    if platform.system() != 'Darwin':
        raise ValueError('macOS 发布包必须在 macOS 上构建')
    architecture = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64'}.get(platform.machine().lower())
    if architecture is None:
        raise ValueError('不支持的 CPU 架构')
    if not binary.is_file():
        raise ValueError('找不到二进制: ' + str(binary))
    runtime = binary.parent / RUNTIME_DIRECTORY
    if not runtime.is_dir():
        raise ValueError('找不到 macOS 运行时目录: ' + str(runtime))
    output.mkdir(parents=True, exist_ok=True)
    # Include the licenses of components embedded by PyInstaller.
    notices = output / 'THIRD_PARTY_NOTICES.txt'
    notices.write_text(third_party_notices(), encoding='utf-8')
    destination = output / 'network-monitor-{}-macos-{}.tar.gz'.format(version, architecture)
    with tarfile.open(destination, 'w:gz') as package:
        package.add(binary, arcname='network-monitor')
        package.add(runtime, arcname=RUNTIME_DIRECTORY)
        for name in ('README.md', 'LICENSE', 'network-monitor.example.ini'):
            package.add(ROOT / name, arcname=name)
        package.add(notices, arcname=notices.name)
    print(destination)
    return destination


def generate_formula(assets, output, version):
    check_version(version)
    formula = (ROOT / 'packaging/homebrew/network-monitor.rb.in').read_text(encoding='utf-8')
    checksums = []
    for architecture, placeholder in (('arm64', '@ARM64_SHA256@'), ('x86_64', '@X86_64_SHA256@')):
        path = assets / 'network-monitor-{}-macos-{}.tar.gz'.format(version, architecture)
        with tarfile.open(path, 'r:gz') as package:
            members = {member.name: member for member in package.getmembers()}
            for name in ('network-monitor', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
                if name not in members or not members[name].isfile():
                    raise ValueError('发布包缺少必要文件: ' + name)
            if RUNTIME_DIRECTORY not in members or not members[RUNTIME_DIRECTORY].isdir():
                raise ValueError('发布包缺少 macOS 运行时目录: ' + RUNTIME_DIRECTORY)
            if not members['network-monitor'].mode & 0o111:
                raise ValueError('发布包中的二进制没有执行权限')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        formula = formula.replace(placeholder, digest)
        checksums.append('{}  {}\n'.format(digest, path.name))
    formula = formula.replace('@VERSION@', version)
    for package in sorted(assets.glob('network-monitor_{}-1_*.deb'.format(version))):
        checksums.append('{}  {}\n'.format(hashlib.sha256(package.read_bytes()).hexdigest(), package.name))
    output.mkdir(parents=True, exist_ok=True)
    (output / 'network-monitor.rb').write_text(formula, encoding='utf-8')
    (output / 'SHA256SUMS').write_text(''.join(checksums), encoding='utf-8')


def release_notes(output, version):
    check_version(version)
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    section = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## |\Z)',
                        changelog, re.M | re.S)
    if section is None or not section.group(1).strip():
        raise ValueError('更新日志缺少版本内容: ' + version)
    notes = (ROOT / 'packaging/release-notes.md.in').read_text(encoding='utf-8')
    notes = notes.replace('@VERSION@', version).replace('@CHANGES@', section.group(1).strip())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(notes, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    bundle = commands.add_parser('archive')
    bundle.add_argument('--binary', type=Path, default=ROOT / 'dist/network-monitor/network-monitor')
    bundle.add_argument('--output', type=Path, default=ROOT / 'release')
    deb = commands.add_parser('deb', help='构建 Linux deb 安装包')
    deb.add_argument('--binary', type=Path, default=ROOT / 'dist/network-monitor')
    deb.add_argument('--output', type=Path, default=ROOT / 'release')
    formula = commands.add_parser('formula')
    formula.add_argument('--assets', type=Path, required=True)
    formula.add_argument('--output', type=Path, required=True)
    notes = commands.add_parser('notes')
    notes.add_argument('--output', type=Path, default=Path('release-notes.md'))
    arguments = parser.parse_args()
    try:
        if arguments.command == 'archive':
            archive(arguments.binary, arguments.output, arguments.version)
        elif arguments.command == 'deb':
            from scripts.deb import build
            build(arguments.binary, arguments.output, arguments.version)
        elif arguments.command == 'formula':
            generate_formula(arguments.assets, arguments.output, arguments.version)
        else:
            release_notes(arguments.output, arguments.version)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
