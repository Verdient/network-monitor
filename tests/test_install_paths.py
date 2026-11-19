from contextlib import ExitStack
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configuration
import install_paths
import network_monitor
import service_install


class InstallPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='network-monitor-install-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.root / 'home'), 'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'state'), 'PATH': '/usr/bin:/bin'}))

    def test_platform_and_architecture_defaults(self):
        for system, machine, executable, config in (
            ('Linux', 'aarch64', '/usr/local/bin/network-monitor', '/etc/network-monitor/config.ini'),
            ('Linux', 'x86_64', '/usr/local/bin/network-monitor', '/etc/network-monitor/config.ini'),
            ('Darwin', 'arm64', '/opt/homebrew/bin/network-monitor', '/opt/homebrew/etc/network-monitor/config.ini'),
            ('Darwin', 'aarch64', '/opt/homebrew/bin/network-monitor', '/opt/homebrew/etc/network-monitor/config.ini'),
            ('Darwin', 'x86_64', '/usr/local/bin/network-monitor', '/usr/local/etc/network-monitor/config.ini'),
        ):
            with self.subTest(system=system, machine=machine), \
                    patch('install_paths.platform.system', return_value=system), \
                    patch('install_paths.platform.machine', return_value=machine):
                selected = install_paths.layout()
                self.assertEqual(selected.executable, Path(executable))
                self.assertEqual(selected.config, Path(config))
                self.assertEqual(configuration.search_paths()[1], selected.config)

    def test_user_config_follows_platform_conventions(self):
        with patch('install_paths.platform.system', return_value='Linux'):
            selected = install_paths.layout(user=True)
            self.assertEqual(selected.config, self.root / 'config/network-monitor/config.ini')
        with patch('install_paths.platform.system', return_value='Darwin'):
            selected = install_paths.layout(user=True)
            self.assertEqual(selected.config, self.root / 'home/Library/Application Support/network-monitor/config.ini')
        self.assertEqual(selected.executable, self.root / 'home/.local/bin/network-monitor')

    def test_path_lookup_checks_directory_instead_of_another_same_named_binary(self):
        source = self.root / 'downloads/network-monitor'
        self.assertFalse(install_paths.in_path(source))
        with patch.dict(os.environ, {'PATH': str(source.parent) + os.pathsep + '/usr/bin'}):
            self.assertTrue(install_paths.in_path(source))
        with patch.dict(os.environ, {'PATH': str(source.parent) + '-other'}):
            self.assertFalse(install_paths.in_path(source))

    def test_confirmation_defaults_to_yes_and_can_decline(self):
        with patch('builtins.input', return_value=''):
            self.assertTrue(service_install.confirm_move('move?'))
        with patch('builtins.input', return_value='n'):
            self.assertFalse(service_install.confirm_move('move?'))
        with patch('builtins.input', side_effect=['invalid', 'YES']), patch('sys.stdout', new=io.StringIO()):
            self.assertTrue(service_install.confirm_move('move?'))
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaisesRegex(RuntimeError, '--yes'):
                service_install.confirm_move('move?')

    def setup_frozen(self, *options):
        source = self.root / 'downloads/network-monitor'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'packaged program')
        config = self.root / 'custom.ini'
        config.write_text('[monitor]\ninterval=2\n[stats]\ntimezone=UTC\n', encoding='utf-8')
        self.stack.enter_context(patch('service_install.sys.frozen', True, create=True))
        self.stack.enter_context(patch('service_install.sys.executable', str(source)))
        self.stack.enter_context(patch('service_install.platform.system', return_value='Linux'))
        self.stack.enter_context(patch('service_install.shutil.which', return_value='/usr/bin/systemctl'))
        parser, _ = network_monitor.build_parser()
        args = parser.parse_args(['install', '--user'] + list(options))
        args.config_path = config
        args.config_defaults, _ = configuration.load(config)
        return parser, args, service_install.make_plan(args, parser)

    def test_accepting_both_moves_updates_service_paths_and_removes_sources_on_success(self):
        parser, args, plan = self.setup_frozen()
        with patch('builtins.input', return_value='') as prompt:
            selected = service_install.select_paths(args, parser, plan)
        self.assertEqual(prompt.call_count, 2)
        self.assertTrue(selected.move_executable)
        self.assertTrue(selected.move_config)
        self.assertIn(str(selected.config), selected.service_content.decode())
        with patch('service_install.run_command'):
            service_install.install(selected)
        self.assertFalse(plan.source.exists())
        self.assertFalse(plan.config_source.exists())
        self.assertEqual(selected.executable.read_bytes(), b'packaged program')
        defaults, _ = configuration.load(selected.config)
        self.assertNotIn('count', defaults['monitor'])
        self.assertNotIn('install', defaults)
        self.assertEqual(defaults['stats'], {'timezone': 'UTC'})

    def test_declining_both_moves_keeps_original_service_paths(self):
        parser, args, plan = self.setup_frozen()
        with patch('builtins.input', return_value='n'):
            selected = service_install.select_paths(args, parser, plan)
        self.assertEqual(selected.executable, plan.source)
        self.assertEqual(selected.config, plan.config_source)
        self.assertFalse(selected.move_executable)
        self.assertFalse(selected.move_config)
        with patch('service_install.run_command'):
            service_install.install(selected)
        self.assertTrue(plan.source.exists())
        self.assertTrue(plan.config_source.exists())

    def test_failed_service_start_keeps_original_program_and_config(self):
        parser, args, plan = self.setup_frozen('--yes')
        with patch('builtins.input') as prompt:
            selected = service_install.select_paths(args, parser, plan)
        prompt.assert_not_called()
        def command(argv):
            if 'restart' in argv:
                raise RuntimeError('failed to start')
        with patch('service_install.run_command', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                service_install.install(selected)
        self.assertTrue(plan.source.exists())
        self.assertTrue(plan.config_source.exists())

    def test_binary_already_in_path_is_reused_without_prompt(self):
        parser, args, plan = self.setup_frozen()
        args.config_path = None
        with patch.dict(os.environ, {'PATH': str(plan.source.parent)}):
            plan = service_install.make_plan(args, parser)
            with patch('builtins.input') as prompt:
                selected = service_install.select_paths(args, parser, plan)
        prompt.assert_not_called()
        self.assertEqual(selected.executable, plan.source)
        self.assertFalse(selected.move_executable)

    def test_preview_never_prompts_or_moves(self):
        parser, args, plan = self.setup_frozen('--dry-run')
        with patch('builtins.input') as prompt, patch('service_install.install') as install, \
                patch('sys.stdout', new=io.StringIO()):
            service_install.run(args, parser)
        prompt.assert_not_called()
        install.assert_not_called()
        self.assertTrue(plan.source.exists())
        self.assertTrue(plan.config_source.exists())

    def test_homebrew_marker_uses_custom_prefix_for_configuration_and_data(self):
        prefix = self.root / 'custom-homebrew'
        binary = prefix / 'Cellar/network-monitor/0.1.0/libexec/network-monitor'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'program')
        (binary.parent / 'homebrew-prefix').write_text(str(prefix) + '\n', encoding='utf-8')
        with patch('install_paths.sys.frozen', True, create=True), \
                patch('install_paths.sys.executable', str(binary)):
            self.assertEqual(install_paths.homebrew_prefix(), prefix)
            selected = install_paths.layout()
            self.assertEqual(selected.executable, prefix / 'bin/network-monitor')
            self.assertEqual(selected.config, prefix / 'etc/network-monitor/config.ini')
            self.assertEqual(selected.data, prefix / 'var/lib/network-monitor')
            self.assertEqual(configuration.search_paths()[1], selected.config)

    def test_homebrew_install_command_cannot_move_managed_program(self):
        parser, _ = network_monitor.build_parser()
        args = parser.parse_args(['install', '--yes'])
        with patch('install_paths.homebrew_prefix', return_value=self.root), \
                patch('service_install.install') as install:
            with self.assertRaisesRegex(RuntimeError, 'brew services'):
                service_install.run(args, parser)
        install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
