"""Context-sensitive candidates shared by Bash, Zsh and Fish."""

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import socket
import sqlite3
from zoneinfo import available_timezones

import configuration
import storage
import time_ranges


SCRIPTS = {
    'bash': r'''# Bash completion for network-monitor
_network_monitor_complete() {
    local mode match
    COMPREPLY=()
    {
        IFS= read -r mode || return 0
        while IFS= read -r match; do
            COMPREPLY+=("$match")
        done
    } < <("$1" __complete bash "$1" "${COMP_LINE:0:COMP_POINT}" "$COMP_WORDBREAKS" 2>/dev/null)
    if [[ $mode == words ]]; then
        compopt +o filenames 2>/dev/null
    elif [[ $mode == path && ${#COMPREPLY[@]} == 1 && ${COMPREPLY[0]} == */ ]]; then
        compopt -o nospace 2>/dev/null
    fi
    return 0
}
complete -o filenames -F _network_monitor_complete network-monitor
''',
    'zsh': r'''#compdef network-monitor
_network_monitor_complete() {
    local -a previous matches directories
    local match mode
    previous=("${(@)words[2,CURRENT-1]}")
    matches=("${(@f)$(command "${(Q)words[1]}" __complete words "${(Q)PREFIX}" "${(@Q)previous}" 2>/dev/null)}")
    mode=$matches[1]
    shift matches
    (( $#matches )) || return 0
    if [[ $mode == path ]]; then
        for match in "${matches[@]}"; do
            [[ $match == */ ]] && directories+=("$match")
        done
        matches=("${(@)matches:#*/}")
        (( $#directories )) && compadd -U -S '' -- "${directories[@]}"
    fi
    (( $#matches )) && compadd -U -- "${matches[@]}"
    return 0
}
if [[ ${funcstack[1]} == _network[-_]monitor ]]; then
    _network_monitor_complete "$@"
else
    compdef _network_monitor_complete network-monitor
fi
''',
    'fish': r'''# Fish completion for network-monitor
function __network_monitor_complete
    set -l previous (commandline -opc)
    set -l executable $previous[1]
    set -l token (commandline -ct)
    set -l prefix (string unescape -- "$token")
    if test (count $prefix) -eq 0
        set prefix (string replace -r "^['\"]" '' -- "$token")
    end
    command $executable __complete words "$prefix" $previous[2..-1] 2>/dev/null | tail -n +2
end
complete -c network-monitor -f -a '(__network_monitor_complete)'
''',
}

# Bash supplies a full command line because Readline splits words at characters
# like "=" and ":" even when they are part of a single CLI argument.
BASH_PARTS = re.compile(
    r"""(?P<single>'[^']*'?)|(?P<double>"(?:\\.|[^"\\])*"?)|(?P<escape>\\.?)"""
    r"""|(?P<separator>[;&|()<>])|(?P<space>\s+)|(?P<plain>[^'"\\;&|()<>\s]+)""", re.S
)


def bash_words(executable, line, breaks):
    words = []
    current = replaced_prefix = ''
    in_word = False
    for part in BASH_PARTS.finditer(line):
        kind = part.lastgroup
        value = part[0]
        if kind in ('space', 'separator'):
            if in_word:
                words.append(current)
            if kind == 'separator':
                words.clear()
            current = replaced_prefix = ''
            in_word = False
            continue
        in_word = True
        if kind == 'single':
            current += value[1:].removesuffix("'")
        elif kind == 'double':
            body = value[1:]
            if re.fullmatch(r'(?:\\.|[^"\\])*"', body, re.S):
                body = body[:-1]
            current += re.sub(r'\\([\\"$\x60\n])', lambda match: match[1].removesuffix('\n'), body)
        elif kind == 'escape':
            current += value[1:].removesuffix('\n')
        else:
            for index in range(len(value) - 1, -1, -1):
                if value[index] in breaks:
                    replaced_prefix = current + value[:index + 1]
                    break
            current += value

    for index, word in enumerate(words):
        if os.path.basename(word) == os.path.basename(executable):
            return words[index + 1:], current, replaced_prefix
    return [], '', ''


def find_option(parser, word):
    options = parser._option_string_actions
    option, separator, value = word.partition('=')
    if option in options:
        return options[option], value if separator else None, option + separator
    # argparse also accepts attached short values, for example -ieth0.
    action = options.get(word[:2]) if len(word) > 2 and not word.startswith('--') else None
    if action is not None and action.nargs != 0:
        return action, word[2:], word[:2]
    return None, None, ''


def looks_like_option(parser, word):
    return (word.startswith('-') and word != '-'
            and not parser._negative_number_matcher.match(word))


def argument_context(root, commands, words):
    parser = root
    command = None
    values = {}
    used = set()
    pending = None
    consumed = False
    for word in words:
        if pending is not None and not looks_like_option(parser, word):
            values.setdefault(pending.dest, []).append(word)
            consumed = True
            if pending.nargs != '+':
                pending = None
            continue
        if word == '--':
            return None, command, used, values, None, False
        action, attached, _ = find_option(parser, word)
        pending = None
        consumed = False
        if action is not None:
            if isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
                return None, command, used, values, None, False
            used.add(action.dest)
            if action.nargs != 0:
                if attached is None:
                    pending = action
                else:
                    values.setdefault(action.dest, []).append(attached)
        elif parser is root and word in commands:
            command = word
            parser = commands[word]
            used.clear()
            # Only the global configuration selection carries into a subcommand.
            values = {key: vals for key, vals in values.items() if key == 'config'}
        elif command == 'completion':
            return None, command, used, values, None, False
    return parser, command, used, values, pending, consumed


def paths(prefix, directories_only):
    if prefix == '~':
        return ['~/']
    directory, separator, name = prefix.rpartition('/')
    head = directory + separator
    folder = os.path.expanduser(head) or '.'
    try:
        with os.scandir(folder) as entries:
            matches = []
            for entry in entries:
                if not entry.name.startswith(name) or (entry.name.startswith('.') and not name.startswith('.')):
                    continue
                is_dir = entry.is_dir()
                if is_dir or not directories_only:
                    matches.append(head + entry.name + ('/' if is_dir else ''))
    except (OSError, ValueError):
        return []
    return sorted(matches)


def last_value(values, name, default=None):
    return values.get(name, [default])[-1]


def config_defaults(values):
    try:
        return configuration.load(last_value(values, 'config'))[0]
    except ValueError:
        return {}


def interfaces(command, values, prefix, repeated):
    try:
        names = {name for _, name in socket.if_nameindex()}
    except OSError:
        try:
            names = {entry.name for entry in Path('/sys/class/net').iterdir()}
        except OSError:
            names = set()
    if command == 'stats':
        defaults = config_defaults(values).get('monitor', {})
        database = last_value(values, 'database', defaults.get('database', storage.DEFAULT_DATABASE))
        try:
            with storage.reader(database, timeout=0.05) as connection:
                names.update(name for name, in connection.execute('SELECT DISTINCT interface FROM partitions'))
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            pass
    head, separator, fragment = prefix.rpartition(',') if repeated else ('', '', prefix)
    selected = {name.strip() for item in values.get('interface', []) for name in item.split(',')} if repeated else set()
    selected.update(name.strip() for name in head.split(','))
    return [head + separator + name for name in sorted(names.difference(selected)) if name.startswith(fragment)]


def option_values(action, command, values, prefix):
    dest = action.dest
    if action.choices is not None:
        return 'words', action.choices
    if dest in ('database', 'config', 'work_dir'):
        return 'path', paths(prefix, dest == 'work_dir')
    if dest == 'interface':
        return 'words', interfaces(command, values, prefix, action.nargs == '+')
    if dest == 'timezone':
        return 'words', sorted(available_timezones() | {'UTC', 'local', 'system', '+08:00', '-05:00'})
    if dest == 'last':
        if re.fullmatch(r'[1-9][0-9]*', prefix):
            return 'words', [prefix + unit for unit in 'smhd']
        return 'words', ('30s', '5m', '15m', '30m', '1h', '2h', '1d', '7d')
    if dest in ('day', 'start', 'end'):
        default_zone = config_defaults(values).get('stats', {}).get('timezone')
        try:
            zone = time_ranges.timezone(last_value(values, 'timezone', default_zone))
        except ValueError:
            zone = None
        today = dt.datetime.now(zone).date()
        return 'words', [(today + dt.timedelta(days=offset)).isoformat() for offset in range(1, -7, -1)]
    return 'words', ()


def option_names(parser, command, used):
    excluded = set(used)
    for action in parser._actions:
        if action.nargs == '+':
            excluded.discard(action.dest)
    for group in parser._mutually_exclusive_groups:
        if any(action.dest in used for action in group._group_actions):
            excluded.update(action.dest for action in group._group_actions)
    if used & {'start', 'end'}:
        excluded.update(('day', 'last'))
    if used & {'day', 'last'}:
        excluded.update(('start', 'end', 'day', 'last'))
    if command == 'stats':
        if 'live' not in used:
            excluded.add('interval')
        if 'traffic' not in used:
            excluded.add('count')
        if used & {'plain', 'traffic'}:
            excluded.add('live')
        if 'live' in used:
            excluded.update(('plain', 'traffic'))
    return [option for option, action in parser._option_string_actions.items()
            if action.dest not in excluded and action.help != argparse.SUPPRESS]


def candidates(root, commands, words, prefix):
    parser, command, used, values, pending, consumed = argument_context(root, commands, words)
    if parser is None:
        return 'words', []
    action, attached, head = find_option(parser, prefix)
    if attached is not None and action.nargs != 0:
        mode, choices = option_values(action, command, values, attached)
        return mode, [head + item for item in choices if item.startswith(attached)]
    if pending is not None:
        option_allowed = pending.nargs == '?' or consumed
        typing_option = looks_like_option(parser, prefix) or prefix == '-'
        if not (option_allowed and typing_option) and (pending.nargs is None or not looks_like_option(parser, prefix)):
            mode, choices = option_values(pending, command, values, prefix)
            return mode, [item for item in choices if item.startswith(prefix)]
    choices = option_names(parser, command, used)
    if parser is root:
        choices = list(commands) + choices
    elif command == 'completion':
        choices += list(SCRIPTS)
    return 'words', [item for item in choices if item.startswith(prefix)]


def respond(root, commands, args):
    trim = ''
    try:
        if len(args) == 4 and args[0] == 'bash':
            words, prefix, trim = bash_words(*args[1:])
        elif len(args) >= 2 and args[0] == 'words':
            prefix, *words = args[1:]
        else:
            return
        mode, matches = candidates(root, commands, words, prefix)
    except (OSError, RuntimeError, ValueError):
        return
    matches = [match[len(trim):] for match in matches
               if match.startswith(trim) and not any(ord(char) < 32 for char in match)]
    if args[0] == 'bash' and mode == 'words' and any(
            not re.fullmatch(r'[A-Za-z0-9_.,/+:=\-]+', match) for match in matches):
        mode = 'quoted'
    print(mode)
    for match in matches:
        print(match)
