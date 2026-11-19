"""Optional, typed INI defaults for the command-line interface."""

import configparser
from pathlib import Path

import install_paths


SCHEMA = {
    'monitor': {'interface': str, 'database': str, 'interval': float},
    'stats': {'timezone': str},
}

CLI_ONLY = {'monitor': {'count'},
            'stats': {'start', 'end', 'database', 'interface', 'no_chart', 'width', 'height'}}


def search_paths():
    return [install_paths.layout(user=True).config, install_paths.layout().config]


def load(path=None):
    explicit = path is not None
    if explicit:
        path = Path(path).expanduser()
    else:
        for candidate in search_paths():
            try:
                if candidate.exists():
                    path = candidate
                    break
            except PermissionError:
                # sudo/runuser may preserve another user's XDG paths. Continue
                # with the system configuration if that directory is private.
                continue
    if path is None:
        return {}, None
    path = path.resolve()
    config = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding='utf-8') as file:
            config.read_file(file)
        if config.defaults():
            raise ValueError('不支持 [DEFAULT]，请使用 [monitor] 或 [stats]')
        defaults = {}
        for section in config.sections():
            if section not in SCHEMA:
                if section == 'install':
                    raise ValueError('[install] 不属于服务配置，安装选项请通过命令行指定')
                raise ValueError('未知配置节 [{}]'.format(section))
            defaults[section] = {}
            for key, raw in config.items(section):
                if key not in SCHEMA[section]:
                    if key in CLI_ONLY.get(section, set()):
                        raise ValueError('[{}] {} 仅支持命令行，不属于长期服务配置'.format(section, key))
                    raise ValueError('未知配置项 [{}] {}'.format(section, key))
                converter = SCHEMA[section][key]
                try:
                    value = config.getboolean(section, key) if converter is bool else converter(raw)
                except ValueError:
                    raise ValueError('[{}] {} 的值无效: {!r}'.format(section, key, raw)) from None
                if converter is str and not value.strip():
                    raise ValueError('[{}] {} 不得为空'.format(section, key))
                defaults[section][key] = value
        return defaults, path
    except (OSError, configparser.Error, ValueError) as error:
        raise ValueError('无法加载配置文件 {}: {}'.format(path, error)) from error
