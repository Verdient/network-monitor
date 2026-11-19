import configparser
from contextlib import ExitStack
import hashlib
import io
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import deb
from version import __version__


@unittest.skipUnless(platform.system() == 'Linux' and shutil.which('dpkg-deb'), '需要 Linux 和 dpkg-deb')
class DebianPackageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.binary = self.root / 'network-monitor'
        self.elf(62)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('scripts.deb.platform.machine', return_value='x86_64'))
        self.stack.enter_context(patch('scripts.deb.platform.libc_ver', return_value=('glibc', '2.31')))
        self.stack.enter_context(patch('scripts.deb.third_party_notices', return_value='runtime licenses'))
        self.stack.enter_context(patch('sys.stdout', new=io.StringIO()))

    def elf(self, machine):
        self.binary.write_bytes(b'\x7fELF\x02\x01' + b'\0' * 12 + struct.pack('<H', machine))
        self.binary.chmod(0o755)

    def build(self):
        return deb.build(self.binary, self.root / 'output', __version__)

    def test_real_deb_has_managed_binary_config_service_and_correct_checksums(self):
        package = self.build()
        self.assertEqual(package.name, 'network-monitor_{}-1_amd64.deb'.format(__version__))
        field = lambda name: subprocess.check_output(['dpkg-deb', '-f', str(package), name], text=True).strip()
        self.assertEqual(field('Architecture'), 'amd64')
        self.assertEqual(field('Version'), __version__ + '-1')
        self.assertIn('libc6 (>= 2.31)', field('Depends'))
        self.assertNotIn('python', field('Depends').lower())
        extracted = self.root / 'extracted'
        subprocess.run(['dpkg-deb', '-R', str(package), str(extracted)], check=True)
        binary = extracted / 'usr/bin/network-monitor'
        self.assertTrue(binary.is_symlink())
        self.assertEqual(binary.read_bytes(), self.binary.read_bytes())
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertTrue((extracted / 'usr/lib/network-monitor/debian-package').is_file())
        self.assertEqual((extracted / 'DEBIAN/conffiles').read_text(), '/etc/network-monitor/config.ini\n')
        config = configparser.ConfigParser()
        config.read(extracted / 'etc/network-monitor/config.ini')
        self.assertEqual(config['monitor']['database'], '/var/lib/network-monitor/network-monitor.db')
        self.assertEqual(config['monitor'].getfloat('interval'), 1)
        unit = (extracted / 'usr/lib/systemd/system/network-monitor.service').read_text()
        self.assertIn('ExecStart=/usr/bin/network-monitor --config /etc/network-monitor/config.ini monitor', unit)
        self.assertIn('User=network-monitor', unit)
        self.assertIn('runtime licenses', (extracted / 'usr/share/doc/network-monitor/copyright').read_text())
        for name in ('postinst', 'prerm', 'postrm'):
            script = extracted / 'DEBIAN' / name
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
            subprocess.run(['sh', '-n', str(script)], check=True)
        for line in (extracted / 'DEBIAN/md5sums').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(hashlib.md5((extracted / name).read_bytes()).hexdigest(), digest)
        contents = subprocess.check_output(['dpkg-deb', '-c', str(package)], text=True)
        self.assertTrue(all('root/root' in line for line in contents.splitlines()))

    def test_arm64_metadata_requires_arm64_elf(self):
        with patch('scripts.deb.platform.machine', return_value='aarch64'):
            with self.assertRaisesRegex(ValueError, 'ELF'):
                self.build()
            self.elf(183)
            package = self.build()
        architecture = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Architecture'], text=True).strip()
        self.assertEqual(architecture, 'arm64')

    def test_rejects_nonlinux_nonelf_and_nonexecutable_inputs(self):
        with patch('scripts.deb.platform.system', return_value='Darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux'):
                self.build()
        self.binary.write_text('#!/bin/sh\n')
        with self.assertRaisesRegex(ValueError, 'ELF'):
            self.build()
        self.elf(62)
        self.binary.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '可执行'):
            self.build()


if __name__ == '__main__':
    unittest.main()
