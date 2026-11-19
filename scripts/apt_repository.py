#!/usr/bin/env python3
"""将正式 Release 的 deb 安装包生成带签名的 APT 源。"""

import argparse
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = ('amd64', 'arm64')
REPOSITORY = 'Verdient/network-monitor'


def run(command, **kwargs):
    result = subprocess.run(command, check=False, capture_output=True, **kwargs)
    if result.returncode:
        message = result.stderr.decode('utf-8', errors='replace')
        raise RuntimeError('命令执行失败: {}\n{}'.format(command[0], message))
    return result.stdout


def download_releases(destination, repository=REPOSITORY):
    pages = json.loads(run(['gh', 'api', '--paginate', '--slurp', 'repos/' + repository + '/releases']))
    found = False
    for release in (release for page in pages for release in page):
        if release['draft'] or release['prerelease']:
            continue
        tag = release['tag_name']
        if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
            continue
        names = [asset['name'] for asset in release['assets']]
        if not any(name.endswith('.deb') for name in names):
            continue
        for architecture in ARCHITECTURES:
            if not any(re.fullmatch(r'network-monitor_' + re.escape(tag[1:])
                                   + r'-\d+_' + architecture + r'\.deb', name) for name in names):
                raise ValueError('{} 缺少 {} 安装包'.format(tag, architecture))
        if 'SHA256SUMS' not in names:
            raise ValueError(tag + ' 缺少 SHA256SUMS')
        directory = destination / tag
        directory.mkdir(parents=True, exist_ok=True)
        run(['gh', 'release', 'download', tag, '--repo', repository, '--dir', str(directory),
             '--pattern', 'network-monitor_*.deb', '--pattern', 'SHA256SUMS'])
        found = True
    if not found:
        raise ValueError('没有包含 deb 安装包的正式 Release')


def packages_from(assets):
    packages = []
    seen = {}
    for checksum_file in sorted(assets.rglob('SHA256SUMS')):
        checksums = {}
        for line in checksum_file.read_text(encoding='utf-8').splitlines():
            match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
            if not match or match[2] in checksums:
                raise ValueError('校验和文件格式有误: ' + str(checksum_file))
            checksums[match[2]] = match[1]
        for package in sorted(checksum_file.parent.glob('*.deb')):
            match = re.fullmatch(r'network-monitor_(\d+\.\d+\.\d+-\d+)_(amd64|arm64)\.deb', package.name)
            if not match:
                raise ValueError('安装包文件名无效: ' + package.name)
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            if checksums.get(package.name) != digest:
                raise ValueError('安装包 SHA-256 校验失败: ' + package.name)
            fields = run(['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture']).decode('utf-8')
            control = dict(line.split(': ', 1) for line in fields.strip().splitlines())
            if (control.get('Package') != 'network-monitor' or control.get('Version') != match[1]
                    or control.get('Architecture') != match[2]):
                raise ValueError('安装包元数据与文件名不一致: ' + package.name)
            if package.name in seen:
                if seen[package.name] != digest:
                    raise ValueError('同名安装包内容不一致: ' + package.name)
                continue
            seen[package.name] = digest
            packages.append((package, match[2]))
    if {architecture for _, architecture in packages} != set(ARCHITECTURES):
        raise ValueError('APT 源必须包含 amd64 和 arm64 两种架构的安装包')
    if set(assets.rglob('*.deb')) != {package for package, _ in packages}:
        raise ValueError('存在未被校验和覆盖或重复的安装包')
    return packages


def gpg(home, *arguments):
    return run(['gpg', '--homedir', str(home), '--batch', '--yes', *arguments])


def fingerprint(home, key_file):
    output = gpg(home, '--with-colons', '--show-keys', str(key_file)).decode('utf-8')
    keys = []
    primary = False
    for line in output.splitlines():
        fields = line.split(':')
        if fields[0] == 'pub':
            primary = True
        elif fields[0] == 'fpr' and primary:
            keys.append(fields[9])
            primary = False
    if len(keys) != 1:
        raise ValueError('签名公钥文件必须只包含一把主密钥')
    return keys[0]


def build(assets, output, home, public_key, expected_fingerprint):
    if output.exists():
        raise ValueError('输出目录已存在，请使用空目录: ' + str(output))
    actual = fingerprint(home, public_key)
    if actual != expected_fingerprint.strip().upper():
        raise ValueError('签名公钥指纹与预期不符')
    packages = packages_from(assets)
    # Complete validation before creating the directory deployed to Pages.
    output.mkdir(parents=True)
    pool = output / 'pool/main/n/network-monitor'
    pool.mkdir(parents=True)
    for package, _ in packages:
        shutil.copyfile(package, pool / package.name)
    suite = output / 'dists/stable'
    for architecture in ARCHITECTURES:
        directory = suite / 'main' / ('binary-' + architecture)
        directory.mkdir(parents=True)
        index = run(['dpkg-scanpackages', '--arch', architecture, '--multiversion',
                     '--hash', 'sha256', 'pool/main'], cwd=output)
        if not index.strip():
            raise ValueError('生成了空的软件包索引: ' + architecture)
        paragraphs = index.rstrip(b'\n').split(b'\n\n')
        versions = [tuple(map(int, re.search(rb'^Version: (\d+)\.(\d+)\.(\d+)-(\d+)$', paragraph,
                                             re.M).groups())) for paragraph in paragraphs]
        # Recreate previous indexes from the retained packages. Cached Release
        # metadata can still fetch its index by hash after a new Pages deploy.
        for version in sorted(set(versions)):
            historical = b'\n\n'.join(paragraph for paragraph, current in zip(paragraphs, versions)
                                       if current <= version) + b'\n\n'
            for name, data in (('Packages', historical), ('Packages.gz', gzip.compress(historical, mtime=0)),
                               ('Packages.xz', lzma.compress(historical))):
                by_hash = directory / 'by-hash/SHA256' / hashlib.sha256(data).hexdigest()
                by_hash.parent.mkdir(parents=True, exist_ok=True)
                by_hash.write_bytes(data)
                if version == max(versions):
                    (directory / name).write_bytes(data)
    release = run(['apt-ftparchive',
                   '-o', 'APT::FTPArchive::Release::Origin=Verdient',
                   '-o', 'APT::FTPArchive::Release::Label=network-monitor',
                   '-o', 'APT::FTPArchive::Release::Suite=stable',
                   '-o', 'APT::FTPArchive::Release::Codename=stable',
                   '-o', 'APT::FTPArchive::Release::Architectures=amd64 arm64',
                   '-o', 'APT::FTPArchive::Release::Components=main',
                   '-o', 'APT::FTPArchive::Release::Acquire-By-Hash=yes',
                   'release', 'dists/stable'], cwd=output)
    (suite / 'Release').write_bytes(release)
    for name, mode in (('InRelease', '--clearsign'), ('Release.gpg', '--detach-sign')):
        gpg(home, '--pinentry-mode', 'loopback', '--passphrase', '', '--local-user', actual,
            '--digest-algo', 'SHA256', '--armor', '--output', str(suite / name), mode, str(suite / 'Release'))
    shutil.copyfile(public_key, output / 'signing-key.asc')
    (output / 'signing-key.fingerprint').write_text(actual + '\n', encoding='utf-8')
    (output / 'index.html').write_text((ROOT / 'packaging/apt/index.html').read_text(encoding='utf-8')
                                      .replace('@FINGERPRINT@', actual), encoding='utf-8')
    print('APT 源已生成，包含 {} 个安装包，签名指纹：{}'.format(len(packages), actual))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=Path('apt-assets'))
    parser.add_argument('--output', type=Path, default=Path('apt-site'))
    parser.add_argument('--download', action='store_true', help='下载所有正式 Release 的 deb 和校验和')
    parser.add_argument('--gnupghome', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, default=ROOT / 'packaging/apt/signing-key.asc')
    parser.add_argument('--fingerprint-file', type=Path, default=ROOT / 'packaging/apt/signing-key.fingerprint')
    args = parser.parse_args()
    try:
        if args.download:
            if args.assets.exists():
                raise ValueError('下载目录已存在，请使用空目录: ' + str(args.assets))
            download_releases(args.assets)
        build(args.assets.resolve(), args.output.resolve(), args.gnupghome.resolve(), args.public_key.resolve(),
              args.fingerprint_file.read_text(encoding='utf-8'))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, 'APT 源生成失败: {}\n'.format(error))


if __name__ == '__main__':
    main()
