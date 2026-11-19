from contextlib import redirect_stdout
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import apt_repository as apt


TOOLS = ('gpg', 'gpgv', 'gpgconf', 'dpkg-deb', 'dpkg-scanpackages', 'apt-ftparchive', 'apt-get')


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), '需要 GPG、Debian 打包和 APT 工具')
class AptRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='network-monitor-apt-test-key-')
        cls.home = Path(cls.keys.name)
        cls.home.chmod(0o700)
        apt.gpg(cls.home, '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
                'APT Repository Test <apt-test@example.invalid>', 'ed25519', 'sign', '0')
        cls.public_key = cls.home / 'public.asc'
        cls.public_key.write_bytes(apt.gpg(cls.home, '--armor', '--export'))
        cls.fingerprint = apt.fingerprint(cls.home, cls.public_key)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['gpgconf', '--homedir', str(cls.home), '--kill', 'gpg-agent'], check=True)
        cls.keys.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='network-monitor-apt-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.assets = self.root / 'assets'
        self.output = self.root / 'site'
        self.add_version('0.2.0-1')

    def add_version(self, version):
        directory = self.assets / ('v' + version.rsplit('-', 1)[0])
        directory.mkdir(parents=True, exist_ok=True)
        checksums = []
        for architecture in apt.ARCHITECTURES:
            with tempfile.TemporaryDirectory() as temporary:
                package = Path(temporary)
                (package / 'DEBIAN').mkdir()
                (package / 'DEBIAN/control').write_text(
                    'Package: network-monitor\nVersion: {}\nArchitecture: {}\n'
                    'Maintainer: Test <test@example.invalid>\nDescription: Test package\n'.format(version, architecture))
                path = directory / 'network-monitor_{}_{}.deb'.format(version, architecture)
                subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(path)],
                               check=True, capture_output=True)
                checksums.append('{}  {}\n'.format(hashlib.sha256(path.read_bytes()).hexdigest(), path.name))
        (directory / 'SHA256SUMS').write_text(''.join(checksums))

    def build(self, output=None, fingerprint=None):
        with redirect_stdout(io.StringIO()):
            apt.build(self.assets, output or self.output, self.home, self.public_key,
                      fingerprint or self.fingerprint)

    def client(self, architecture='amd64', key=None):
        state = self.root / ('client-' + architecture)
        (state / 'lists/partial').mkdir(parents=True, exist_ok=True)
        (state / 'archives/partial').mkdir(parents=True, exist_ok=True)
        source = state / 'sources.list'
        source.write_text('deb [signed-by={}] {} stable main\n'.format(
            key or self.public_key, self.output.as_uri()))
        options = {
            'Dir::Etc::sourcelist': str(source), 'Dir::Etc::sourceparts': '-',
            'Dir::State::lists': str(state / 'lists'), 'Dir::Cache::archives': str(state / 'archives'),
            'Dir::State::status': '/dev/null', 'APT::Architecture': architecture,
            'APT::Architectures': architecture, 'APT::Sandbox::User': pwd.getpwuid(os.getuid()).pw_name,
            'Acquire::Languages': 'none',
        }
        command = ['apt-get']
        for name, value in options.items():
            command += ['-o', name + '=' + value]
        return command

    def test_signed_repository_is_accepted_by_real_apt_for_both_architectures(self):
        self.build()
        for architecture in apt.ARCHITECTURES:
            with self.subTest(architecture=architecture):
                client = self.client(architecture)
                update = subprocess.run(client + ['update'], text=True, capture_output=True)
                self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
                install = subprocess.run(client + ['--simulate', 'install', 'network-monitor'],
                                         text=True, capture_output=True)
                self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
                self.assertIn('0.2.0-1', install.stdout)
                download = subprocess.run(client + ['download', 'network-monitor=0.2.0-1'], cwd=self.root,
                                          text=True, capture_output=True)
                self.assertEqual(download.returncode, 0, download.stdout + download.stderr)
                self.assertTrue((self.root / 'network-monitor_0.2.0-1_{}.deb'.format(architecture)).is_file())

    def test_wrong_trust_key_and_tampered_release_are_rejected_by_apt(self):
        self.build()
        missing = self.root / 'missing-key.asc'
        result = subprocess.run(self.client(key=missing) + ['update'], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        release = self.output / 'dists/stable/InRelease'
        release.write_bytes(release.read_bytes().replace(b'Origin: Verdient', b'Origin: Tampered'))
        result = subprocess.run(self.client() + ['update'], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_versions_remain_installable_and_old_by_hash_indexes_are_retained(self):
        self.build()
        directory = 'dists/stable/main/binary-amd64'
        old = {path.name: path.read_bytes() for path in (self.output / directory / 'by-hash/SHA256').iterdir()}
        self.add_version('0.3.0-1')
        upgraded = self.root / 'upgraded'
        self.build(output=upgraded)
        self.output = upgraded
        for digest, data in old.items():
            self.assertEqual((upgraded / directory / 'by-hash/SHA256' / digest).read_bytes(), data)
        client = self.client()
        result = subprocess.run(client + ['update'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for version in ('0.2.0-1', '0.3.0-1'):
            result = subprocess.run(client + ['--simulate', 'install', 'network-monitor=' + version],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run(client + ['--simulate', 'install', 'network-monitor'], text=True, capture_output=True)
        self.assertIn('0.3.0-1', result.stdout)

    def test_package_tampering_architecture_mismatch_and_wrong_signer_prevent_publish(self):
        with self.assertRaisesRegex(ValueError, '指纹'):
            self.build(fingerprint='0' * 40)
        package = next(self.assets.rglob('*_amd64.deb'))
        package.write_bytes(package.read_bytes() + b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.build()
        self.assertFalse(self.output.exists())
        self.add_version('0.2.0-1')
        arm = next(self.assets.rglob('*_arm64.deb'))
        package.write_bytes(arm.read_bytes())
        checksum = package.parent / 'SHA256SUMS'
        checksum.write_text(''.join('{}  {}\n'.format(hashlib.sha256(path.read_bytes()).hexdigest(), path.name)
                                    for path in sorted(package.parent.glob('*.deb'))))
        with self.assertRaisesRegex(ValueError, '元数据'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_compressed_indexes_public_key_and_checksums_match(self):
        self.build()
        for architecture in apt.ARCHITECTURES:
            directory = self.output / 'dists/stable/main' / ('binary-' + architecture)
            index = (directory / 'Packages').read_bytes()
            self.assertEqual(gzip.decompress((directory / 'Packages.gz').read_bytes()), index)
            self.assertEqual(lzma.decompress((directory / 'Packages.xz').read_bytes()), index)
            self.assertIn(('Architecture: ' + architecture).encode(), index)
        keyring = self.root / 'keyring.gpg'
        keyring.write_bytes(apt.gpg(self.home, '--output', '-', '--dearmor', str(self.public_key)))
        subprocess.run(['gpgv', '--keyring', str(keyring), str(self.output / 'dists/stable/Release.gpg'),
                        str(self.output / 'dists/stable/Release')], check=True, capture_output=True)
        self.assertEqual((self.output / 'signing-key.asc').read_bytes(), self.public_key.read_bytes())
        self.assertNotIn('@FINGERPRINT@', (self.output / 'index.html').read_text())
        self.assertFalse(any(b'BEGIN PGP PRIVATE KEY' in path.read_bytes()
                             for path in self.output.rglob('*') if path.is_file()))

    def test_download_skips_drafts_and_prereleases_but_keeps_stable_versions(self):
        releases = []
        for version, draft, prerelease, debs in (('0.1.0', False, False, False), ('0.2.0', False, False, True),
                                                ('0.3.0', False, True, True), ('0.4.0', True, False, True)):
            names = ['SHA256SUMS'] + (['network-monitor_{}-1_{}.deb'.format(version, arch)
                                     for arch in apt.ARCHITECTURES] if debs else [])
            releases.append({'tag_name': 'v' + version, 'draft': draft, 'prerelease': prerelease,
                             'assets': [{'name': name} for name in names]})
        with patch('scripts.apt_repository.run', side_effect=[json.dumps([releases]).encode(), b'']) as run:
            apt.download_releases(self.root / 'download')
        self.assertEqual(run.call_count, 2)
        self.assertIn('v0.2.0', run.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
