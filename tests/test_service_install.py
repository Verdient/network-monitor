import configparser
from contextlib import ExitStack
import os
from pathlib import Path
import plistlib
import platform
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import configuration
import install_paths
import network_monitor
import service_install


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='network monitor %$ ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'state'),
        }))
        self.stack.enter_context(patch('service_install.shutil.which', return_value='/usr/bin/manager'))
        self.parser, _ = network_monitor.build_parser()

    def plan(self, system='Linux', *options):
        args = self.parser.parse_args(['install'] + list(options))
        args.config_defaults = {}
        with patch('service_install.platform.system', return_value=system):
            return service_install.make_plan(args, self.parser)

    def test_systemd_selects_user_and_system_locations_and_targets(self):
        user = self.plan('Linux', '--user')
        system = self.plan('Linux')
        self.assertEqual(user.service, self.root / 'config/systemd/user/network-monitor.service')
        self.assertIn(b'WantedBy=default.target', user.service_content)
        self.assertEqual(system.service, Path('/etc/systemd/system/network-monitor.service'))
        self.assertIn(b'WantedBy=multi-user.target', system.service_content)
        # Literal paths must survive systemd specifier/environment expansion.
        self.assertIn('%%$$', user.service_content.decode())
        self.assertIn('WorkingDirectory=' + str(user.work_dir).replace('%', '%%'), user.service_content.decode())

    @unittest.skipUnless(platform.system() == 'Linux' and shutil.which('systemd-analyze'),
                         'requires systemd-analyze')
    def test_generated_systemd_service_passes_real_unit_validation(self):
        plan = self.plan('Linux', '--user')
        service_install.install_program(plan)
        service_install.atomic_write(plan.service, plan.service_content, 0o644)
        result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(plan.service)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launchd_plist_keeps_paths_as_arguments_and_selects_domain(self):
        user = self.plan('Darwin', '--user')
        system = self.plan('Darwin')
        plist = plistlib.loads(user.service_content)
        self.assertEqual(plist['ProgramArguments'], [str(user.executable), '--config', str(user.config), 'monitor'])
        self.assertEqual(plist['WorkingDirectory'], str(user.work_dir))
        self.assertTrue(plist['RunAtLoad'])
        self.assertEqual(plist['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(user.service.parent, self.root / 'home/Library/LaunchAgents')
        self.assertTrue(user.domain.startswith('gui/'))
        self.assertEqual(system.domain, 'system')
        self.assertEqual(system.service.parent, Path('/Library/LaunchDaemons'))

    def test_saved_monitor_config_uses_absolute_data_paths(self):
        plan = self.plan('Linux', '--user', '-i', 'eth0', 'wlan0', '-d', 'logs/traffic.db', '--interval', '2')
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(plan.config_text)
        self.assertEqual(config['monitor']['database'], str(plan.work_dir / 'logs/traffic.db'))
        self.assertEqual(config['monitor']['interface'], 'eth0, wlan0')
        self.assertEqual(config['monitor'].getfloat('interval'), 2)
        self.assertNotIn('count', config['monitor'])
        self.assertNotIn('install', config)

    def test_unknown_system_or_missing_manager_fails(self):
        with self.assertRaisesRegex(RuntimeError, '仅支持'):
            self.plan('Windows')
        with patch('service_install.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'systemctl'):
                self.plan('Linux')

    def test_system_install_requires_root_before_writing(self):
        plan = self.plan('Linux')
        with patch('service_install.os.geteuid', return_value=1000), \
                patch('service_install.run_command') as command:
            with self.assertRaisesRegex(RuntimeError, 'sudo'):
                service_install.install(plan)
            command.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unavailable_manager_fails_without_writing(self):
        plan = self.plan('Linux', '--user')
        with patch('service_install.run_command', side_effect=RuntimeError('no user bus')):
            with self.assertRaisesRegex(RuntimeError, 'no user bus'):
                service_install.install(plan)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_systemd_install_copies_sources_and_restarts_on_reinstall(self):
        plan = self.plan('Linux', '--user')
        with patch('service_install.run_command') as command:
            service_install.install(plan)
            service_install.install(plan)
        commands = [call.args[0] for call in command.call_args_list]
        self.assertEqual(commands.count(['systemctl', '--user', 'daemon-reload']), 2)
        self.assertEqual(commands.count(['systemctl', '--user', 'enable', service_install.UNIT]), 2)
        self.assertEqual(commands.count(['systemctl', '--user', 'restart', service_install.UNIT]), 2)
        self.assertEqual(plan.service.read_bytes(), plan.service_content)
        defaults, _ = configuration.load(plan.config)
        self.assertEqual(defaults['monitor']['database'], str(plan.database))
        # The copied source installation must work outside the original checkout.
        help_result = subprocess.run([str(plan.executable), '-h'], cwd=self.root,
                                     capture_output=True, text=True, timeout=30)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('可用命令', help_result.stdout)

    def test_launchd_reinstall_unloads_before_bootstrapping(self):
        plan = self.plan('Darwin', '--user')
        with patch('service_install.run_command') as command, \
                patch('service_install.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            service_install.install(plan)
        commands = [call.args[0] for call in command.call_args_list]
        target = plan.domain + '/' + service_install.LABEL
        self.assertLess(commands.index(['launchctl', 'bootout', target]),
                        commands.index(['launchctl', 'bootstrap', plan.domain, str(plan.service)]))
        self.assertEqual(plistlib.loads(plan.service.read_bytes())['Label'], service_install.LABEL)

    def test_launchd_fresh_install_does_not_bootout_missing_service(self):
        plan = self.plan('Darwin', '--user')
        with patch('service_install.run_command') as command, \
                patch('service_install.subprocess.run', return_value=subprocess.CompletedProcess([], 113)):
            service_install.install(plan)
        self.assertFalse(any(call.args[0][1] == 'bootout' for call in command.call_args_list))

    def test_frozen_install_copies_executable_and_sets_execute_permission(self):
        plan = self.plan('Linux', '--user')
        source = self.root / 'binary'
        source.write_bytes(b'packaged executable')
        with patch('service_install.sys.frozen', True, create=True), \
                patch('service_install.sys.executable', str(source)):
            service_install.install_program(plan)
        self.assertEqual(plan.executable.read_bytes(), source.read_bytes())
        self.assertEqual(plan.executable.stat().st_mode & 0o777, 0o755)

    def test_bundle_install_preserves_runtime_and_symlinks(self):
        source = self.root / 'network-monitor'
        source.write_bytes(b'executable')
        source.chmod(0o755)
        runtime = self.root / install_paths.RUNTIME_DIRECTORY
        runtime.mkdir()
        (runtime / 'library').write_bytes(b'runtime')
        (runtime / 'linked-library').symlink_to('library')
        for system in ('Darwin', 'Linux'):
            with self.subTest(system=system):
                plan = self.plan(system, '--user')
                with patch('service_install.sys.frozen', True, create=True), \
                        patch('service_install.sys.executable', str(source)), \
                        patch('service_install.sys._MEIPASS', str(runtime), create=True):
                    service_install.install_program(plan)
                installed_runtime = plan.executable.resolve().parent / install_paths.RUNTIME_DIRECTORY
                self.assertTrue(plan.executable.is_symlink())
                self.assertEqual(plan.executable.read_bytes(), b'executable')
                self.assertEqual((installed_runtime / 'linked-library').readlink(), Path('library'))
                self.assertEqual((installed_runtime / 'linked-library').read_bytes(), b'runtime')


if __name__ == '__main__':
    unittest.main()
