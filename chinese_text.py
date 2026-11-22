"""Simplify Chinese geographic names using OpenCC 1.1.9 dictionaries."""

from functools import lru_cache
from pathlib import Path
import re


OPENCC_DATA = Path(__file__).with_name('geo_data') / 'opencc'


def read_dictionary(name):
    entries = {}
    for line in (OPENCC_DATA / name).read_text(encoding='utf-8').splitlines():
        source, targets = line.split('\t', 1)
        entries[source] = targets.split()[0]
    return entries


@lru_cache(maxsize=1)
def conversion_tables():
    characters = str.maketrans(read_dictionary('TSCharacters.txt'))
    phrases = read_dictionary('TSPhrases.txt')
    # Preserve names like 乾县 when a previously simplified label is read from cache.
    for target in tuple(phrases.values()):
        if target.translate(characters) != target:
            phrases.setdefault(target, target)
    pattern = re.compile('|'.join(map(re.escape, sorted(phrases, key=len, reverse=True))))
    return characters, phrases, pattern


def simplify_chinese(text):
    if text.isascii():
        return text
    characters, phrases, pattern = conversion_tables()
    parts = []
    start = 0
    for match in pattern.finditer(text):
        parts.extend((text[start:match.start()].translate(characters), phrases[match[0]]))
        start = match.end()
    parts.append(text[start:].translate(characters))
    return ''.join(parts)
