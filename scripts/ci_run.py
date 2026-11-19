#!/usr/bin/env python3
"""执行 CI 命令，失败时将日志摘要附加到 GitHub 检查。"""

from collections import deque
import os
import subprocess
import sys


def main():
    if len(sys.argv) < 2:
        raise SystemExit('请指定需要执行的命令')
    tail = deque(maxlen=100)
    process = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors='replace', bufsize=1)
    for line in process.stdout:
        print(line, end='', flush=True)
        tail.append(line)
    code = process.wait()
    if code and os.environ.get('GITHUB_ACTIONS') == 'true':
        message = ''.join(tail)[-16000:]
        message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::error title=构建或安装验证失败::' + message, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
