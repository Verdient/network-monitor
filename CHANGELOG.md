# 更新日志

## [0.1.0](https://github.com/Verdient/network-monitor/releases/tag/v0.1.0) - 2026-11-19

- 支持通过 Homebrew 安装，并提供 macOS 14+ 的 Apple Silicon 和 Intel 独立可执行文件。
- 使用 `monitor` 同时采集多张网卡，采样间隔最小为 1 秒。
- 采样数据保存在本地 SQLite 数据库，按网卡和日期分表，保留历史数据。
- 使用 `stats` 查看上传、下载流量及平均、峰值、最低速度，支持终端趋势图。
- 默认统计系统时区当天的数据，支持指定网卡、时间范围和时区。
- 支持配置文件，命令行参数优先于配置文件和默认值。
- 支持通过 Homebrew 管理后台采集，也可使用 `install` 安装 macOS launchd 或 Linux systemd 服务。
