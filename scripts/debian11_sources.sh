#!/bin/sh
# 仅用于 Debian 11 构建和测试容器；LTS 安全更新已迁往归档源。
set -eu
cat > /etc/apt/sources.list <<'EOF'
deb http://deb.debian.org/debian bullseye main
deb [check-valid-until=no] http://archive.debian.org/debian-security bullseye-security main
EOF
rm -f /etc/apt/sources.list.d/debian.sources
