#!/bin/sh
# 在 Debian 基线容器中验证安装包无需 Python，也无需运行中的 systemd。
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends /work/release/*.deb
if command -v python3 >/dev/null; then
    echo '基线镜像不应包含 Python' >&2
    exit 1
fi
network-monitor --version
runuser -u network-monitor -- network-monitor monitor -i lo --count 3 &
collector=$!
sleep 2
runuser -u nobody -- network-monitor stats -i lo --no-chart
wait "$collector"
runuser -u nobody -- network-monitor stats -i lo --no-chart
apt-get purge -y network-monitor
test ! -e /etc/network-monitor/config.ini
test -f /var/lib/network-monitor/network-monitor.db
