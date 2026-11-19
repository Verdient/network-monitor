#!/usr/bin/env python3
"""Run the suite and attach failure tracebacks to GitHub checks."""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        for test, traceback in result.failures + result.errors:
            message = '{}\n{}'.format(test.id(), traceback)
            message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::error title=Test failure::' + message, flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
