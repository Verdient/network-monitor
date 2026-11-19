import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import release
from version import __version__


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def assets(self, omit=None, executable=True):
        for architecture in ('arm64', 'x86_64'):
            path = self.root / 'network-monitor-{}-macos-{}.tar.gz'.format(__version__, architecture)
            with tarfile.open(path, 'w:gz') as package:
                for name in ('network-monitor', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
                    if name == omit:
                        continue
                    data = (name + architecture).encode('utf-8')
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o755 if name == 'network-monitor' and executable else 0o644
                    package.addfile(member, io.BytesIO(data))

    def test_formula_uses_the_actual_archive_hashes_and_release_urls(self):
        self.assets()
        output = self.root / 'formula'
        release.generate_formula(self.root, output, __version__)
        formula = (output / 'network-monitor.rb').read_text()
        checksums = (output / 'SHA256SUMS').read_text()
        self.assertIn('license "0BSD"', formula)
        self.assertIn('service do', formula)
        self.assertIn('homebrew-prefix', formula)
        for archive in self.root.glob('*.tar.gz'):
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertIn('sha256 "{}"'.format(digest), formula)
            self.assertIn('releases/download/v{}/{}'.format(__version__, archive.name), formula)
            self.assertIn('{}  {}\n'.format(digest, archive.name), checksums)
        self.assertNotIn('@VERSION@', formula)
        self.assertNotIn('@ARM64_SHA256@', formula)

    def test_missing_license_prevents_formula_generation(self):
        self.assets(omit='LICENSE')
        with self.assertRaisesRegex(ValueError, 'LICENSE'):
            release.generate_formula(self.root, self.root / 'formula', __version__)

    def test_release_checksums_include_debian_packages(self):
        self.assets()
        packages = []
        for architecture in ('amd64', 'arm64'):
            package = self.root / 'network-monitor_{}-1_{}.deb'.format(__version__, architecture)
            package.write_bytes(architecture.encode('ascii'))
            packages.append(package)
        output = self.root / 'formula'
        release.generate_formula(self.root, output, __version__)
        checksums = (output / 'SHA256SUMS').read_text()
        for package in packages:
            self.assertIn('{}  {}\n'.format(hashlib.sha256(package.read_bytes()).hexdigest(), package.name), checksums)

    def test_nonexecutable_binary_prevents_formula_generation(self):
        self.assets(executable=False)
        with self.assertRaisesRegex(ValueError, '执行权限'):
            release.generate_formula(self.root, self.root / 'formula', __version__)

    def test_release_version_must_match_application(self):
        for version in ('v' + __version__, '99.99.99', 'invalid'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.check_version(version)

    def test_notes_select_matching_changelog_and_use_versioned_downloads(self):
        (self.root / 'packaging').mkdir()
        (self.root / 'packaging/release-notes.md.in').write_text(
            (release.ROOT / 'packaging/release-notes.md.in').read_text(encoding='utf-8'), encoding='utf-8')
        (self.root / 'CHANGELOG.md').write_text(
            '# 更新日志\n\n## [未发布]\n\n- 不应混入的内容\n\n'
            '## [{}](https://example.com) - 2026-11-19\n\n- 本次新增功能\n\n'
            '## [0.0.0]\n\n- 历史功能\n'.format(__version__), encoding='utf-8')
        output = self.root / 'notes.md'
        with patch('scripts.release.ROOT', self.root):
            release.release_notes(output, __version__)
        notes = output.read_text(encoding='utf-8')
        self.assertIn('本次新增功能', notes)
        self.assertNotIn('不应混入的内容', notes)
        self.assertNotIn('历史功能', notes)
        self.assertNotIn('@VERSION@', notes)
        self.assertNotIn('@CHANGES@', notes)
        for architecture in ('arm64', 'x86_64'):
            self.assertIn('releases/download/v{0}/network-monitor-{0}-macos-{1}.tar.gz'.format(
                __version__, architecture), notes)
        self.assertIn('CHANGELOG.md', notes)

    def test_notes_require_changelog_for_release_version(self):
        for content in ('# 更新日志\n', '## [{}]\n\n'.format(__version__)):
            with self.subTest(content=content):
                (self.root / 'CHANGELOG.md').write_text(content, encoding='utf-8')
                output = self.root / 'notes.md'
                with patch('scripts.release.ROOT', self.root), self.assertRaisesRegex(ValueError, '更新日志'):
                    release.release_notes(output, __version__)
                self.assertFalse(output.exists())

    def test_archive_has_binary_project_license_and_runtime_notices(self):
        binary = self.root / 'program'
        binary.write_bytes(b'binary')
        binary.chmod(0o755)
        with patch('scripts.release.platform.system', return_value='Darwin'), \
                patch('scripts.release.platform.machine', return_value='arm64'), \
                patch('scripts.release.third_party_notices', return_value='runtime licenses'), \
                patch('sys.stdout', new=io.StringIO()):
            archive = release.archive(binary, self.root / 'release', __version__)
        self.assertEqual(archive.name, 'network-monitor-{}-macos-arm64.tar.gz'.format(__version__))
        with tarfile.open(archive) as package:
            self.assertEqual(set(package.getnames()), {'network-monitor', 'LICENSE', 'README.md',
                                                       'network-monitor.example.ini', 'THIRD_PARTY_NOTICES.txt'})
            self.assertEqual(package.extractfile('network-monitor').read(), b'binary')
            self.assertEqual(package.extractfile('THIRD_PARTY_NOTICES.txt').read(), b'runtime licenses')

    def test_archive_refuses_to_label_linux_binary_as_macos(self):
        with patch('scripts.release.platform.system', return_value='Linux'):
            with self.assertRaisesRegex(ValueError, 'macOS'):
                release.archive(self.root / 'program', self.root, __version__)


if __name__ == '__main__':
    unittest.main()
