# 更新日志

## [0.3.0](https://github.com/Verdient/network-monitor/releases/tag/v0.3.0) - 2026-11-21

- 直接运行 `network-monitor` 即可打开实时仪表盘。使用 `network-monitor stats` 查询历史统计。
- 实时趋势结合 SQLite 历史和当前会话采样。上传、下载使用各自的纵轴，终端缩放时自动调整布局。
- 按 `c` 查看本机 TCP 连接。按 `i` 查看所选网卡各对端 IP 的实时速率和累计流量，IP 页需要 `tcpdump` 和抓包权限。
- 按 `+` 延长刷新间隔，按 `-` 缩短刷新间隔，每次调整 0.1 秒，最短为 0.1 秒。也可通过 `--interval` 指定初始间隔。

## [0.2.2](https://github.com/Verdient/network-monitor/releases/tag/v0.2.2) - 2026-11-20

- 修复 Linux 的 `.deb` 版本每次启动都解包运行时造成的延迟。帮助、版本查询和统计直接加载安装好的运行时。

## [0.2.1](https://github.com/Verdient/network-monitor/releases/tag/v0.2.1) - 2026-11-20

- 修复 macOS 命令启动缓慢的问题，帮助、版本查询和采集统计直接使用已安装的 Python 运行时。

## [0.2.0](https://github.com/Verdient/network-monitor/releases/tag/v0.2.0) - 2026-11-19

- 新增 Linux amd64 和 arm64 的 deb 安装包，支持 Debian 11+、Ubuntu 20.04+，无需安装 Python。
- 安装包包含 systemd 服务，安装后自动开始采集，支持开机启动。
- 升级保留配置、历史数据和服务禁用状态；卸载停止服务并保留数据。
- 支持普通用户查询后台服务采集的数据，包括服务停止后的历史数据。
- 下载页面提供 Linux 架构徽章，校验和覆盖 macOS 和 Linux 安装包。

## [0.1.0](https://github.com/Verdient/network-monitor/releases/tag/v0.1.0) - 2026-11-19

- 支持通过 Homebrew 安装，并提供 macOS 14+ 的 Apple Silicon 和 Intel 独立可执行文件。
- 使用 `monitor` 同时采集多张网卡，采样间隔最小为 1 秒。
- 采样数据保存在本地 SQLite 数据库，按网卡和日期分表，保留历史数据。
- 使用 `stats` 查看上传、下载流量及平均、峰值、最低速度，支持终端趋势图。
- 默认统计系统时区当天的数据，支持指定网卡、时间范围和时区。
- 支持配置文件，命令行参数优先于配置文件和默认值。
- 支持通过 Homebrew 管理后台采集，也可使用 `install` 安装 macOS launchd 或 Linux systemd 服务。
