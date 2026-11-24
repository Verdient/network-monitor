# 更新日志

## [0.3.5](https://github.com/Verdient/network-monitor/releases/tag/v0.3.5) - 2026-11-24

- 修复 `stats --day` 的历史趋势图横轴。按所选时区显示当天零点到次日零点，无采样的时段留空，支持夏令时切换日。

## [0.3.4](https://github.com/Verdient/network-monitor/releases/tag/v0.3.4) - 2026-11-23

- IP 页优先使用本地 DB-IP City Lite 和 ASN Lite 数据，每天检查一次更新。更新失败时，仅当构建和下载时间均超过 90 天才视为过期。
- 实时和历史仪表盘分别显示 LIVE 和 ARCHIVE。分周期流量汇总在终端中加入上下行分布、峰值、当前周期和合计。
- 补齐源码安装时 IP 页和连接页所需的 GeoIP 文件，移除 Homebrew 配方中弃用的安装钩子。

## [0.3.3](https://github.com/Verdient/network-monitor/releases/tag/v0.3.3) - 2026-11-22

- TCP 页按本地 IP 汇总入站和出站连接、临时端口利用率和估计余量。多地址网卡可先看总览，再按数字键或点击地址查看各出站目标。
- IP 页可在本机和对端模式之间切换。每个地址独立显示上下行速率和累计流量，并按用途标记默认网关和常见 IPv6 组播地址。
- 更新 TCP 和 IP 页布局，增加速率对比和端口余量图示。
- `-v` 可查看版本。Bash、Zsh 和 Fish 支持按命令上下文补全子命令、参数、路径和常用取值。

## [0.3.2](https://github.com/Verdient/network-monitor/releases/tag/v0.3.2) - 2026-11-22

- 修复 Ubuntu 26.04 上使用自定义终端波特率时实时仪表盘启动报错的问题。Linux amd64 和 arm64 在设置即时按键前探测终端接口，并在退出时恢复原设置。
- 恢复 `TERM=xterm` 等基础 ANSI 终端上下载、上传和合计的配色。
- Linux 汇总当前进程可读取的网络命名空间中的 TCP 连接，包括宿主机上可见的 Docker 容器。读取不完整时标记「部分」。
- `stats --traffic` 从首个有采样的周期开始显示，之后缺少采样的周期继续显示为 `—`。

## [0.3.1](https://github.com/Verdient/network-monitor/releases/tag/v0.3.1) - 2026-11-22

- 新增 `-d` / `--day` 查询指定日期，以及 `stats --traffic` 按天、小时、分钟、秒或月汇总下载、上传和合计流量。
- 实时主页在窗口空间充足、抓包成功时显示 IP 流量表。各列表头支持点击排序，分页可点击或使用 `p`、`n`，到达首尾页时禁用对应按钮。
- IP 流量表支持本地识别局域网，显示公网对端的地域、运营商、AS 号和 DNS 反查域名。地域查询使用六个 HTTPS 数据源，结果和限流记录跨会话缓存。
- 中国大陆和美国 IP 支持城市级显示。常见城市和运营商名称统一，繁体地域和运营商名称转成简体。
- TCP 连接趋势改用折线图。采样点从右侧进入，铺满后按时间压缩，纵轴采用整数刻度并标注峰值。
- 优化实时布局、鼠标按钮和明暗主题配色。帮助选项按名称排列，时区显示名称并标记系统时区。

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
