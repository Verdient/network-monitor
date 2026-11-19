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

    def test_nonexecutable_binary_prevents_formula_generation(self):
        self.assets(executable=False)
        with self.assertRaisesRegex(ValueError, '执行权限'):
            release.generate_formula(self.root, self.root / 'formula', __version__)

    def test_release_version_must_match_application(self):
        for version in ('v' + __version__, '99.99.99', 'invalid'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.check_version(version)

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
