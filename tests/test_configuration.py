from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configuration


class ConfigurationDiscoveryTests(unittest.TestCase):
    def test_inaccessible_user_directory_does_not_hide_system_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            system = Path(directory) / 'config.ini'
            system.write_text('[monitor]\ninterval = 3\n', encoding='utf-8')
            private = Path(directory) / 'private/config.ini'
            original = Path.exists

            def exists(path):
                if path == private:
                    raise PermissionError('private home directory')
                return original(path)

            with patch('configuration.search_paths', return_value=[private, system]), \
                    patch.object(Path, 'exists', exists):
                defaults, selected = configuration.load()
            self.assertEqual(selected, system)
            self.assertEqual(defaults, {'monitor': {'interval': 3}})

    def test_explicit_unreadable_configuration_reports_an_error(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private config')):
            with self.assertRaisesRegex(ValueError, '无法加载配置文件'):
                configuration.load('/private/config.ini')


if __name__ == '__main__':
    unittest.main()
