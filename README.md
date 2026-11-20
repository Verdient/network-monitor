# network-monitor

记录网卡的上传、下载速度和累计流量，查看每天或指定时间段的平均网速、峰值与趋势图。
支持同时采集多张网卡，数据保存在本机，重启后可继续查询。

## 按操作系统下载

| 操作系统 | 下载 |
| --- | --- |
| macOS 14+ | [![下载 Apple Silicon（ARM64）版](https://img.shields.io/badge/tar.gz-Apple%20Silicon-000000.svg?logo=apple&logoColor=white)](https://github.com/Verdient/network-monitor/releases/download/v0.2.0/network-monitor-0.2.0-macos-arm64.tar.gz)<br>[![下载 Intel（x86_64）版](https://img.shields.io/badge/tar.gz-Intel%20x64-00A9E0.svg?logo=apple&logoColor=white)](https://github.com/Verdient/network-monitor/releases/download/v0.2.0/network-monitor-0.2.0-macos-x86_64.tar.gz) |
| Linux（Debian 11+ / Ubuntu 20.04+） | [![下载 Linux amd64 版](https://img.shields.io/badge/deb-amd64-A81D33.svg?logo=debian&logoColor=white)](https://github.com/Verdient/network-monitor/releases/download/v0.2.0/network-monitor_0.2.0-1_amd64.deb)<br>[![下载 Linux arm64 版](https://img.shields.io/badge/deb-arm64-D70A53.svg?logo=debian&logoColor=white)](https://github.com/Verdient/network-monitor/releases/download/v0.2.0/network-monitor_0.2.0-1_arm64.deb) |

macOS 的 tar.gz 下载包解压后，运行 `./network-monitor monitor` 开始采集，运行 `./network-monitor stats` 查看统计。请把 `network-monitor-runtime` 目录与可执行文件保留在同一目录。
Linux 下载 deb 安装包后，按下面的步骤安装。
可使用同一版本的 [SHA256SUMS](https://github.com/Verdient/network-monitor/releases/download/v0.2.0/SHA256SUMS) 校验下载文件。

完整更新记录：[ChangeLog](CHANGELOG.md)。

## 安装

### macOS：Homebrew

需要 macOS 14 或更新版本，支持 Apple Silicon 和 Intel。先安装 [Homebrew](https://brew.sh/)，然后运行：

```bash
brew install Verdient/tap/network-monitor
```

确认安装成功：

```bash
network-monitor --version
```

### Linux：APT 软件源

支持 Debian 11+、Ubuntu 20.04+ 的 amd64 和 arm64 系统。首次安装时添加软件源：

```bash
sudo apt update
sudo apt install ca-certificates curl
sudo install -d -m 0755 /etc/apt/keyrings

curl -fsSL https://verdient.github.io/network-monitor/signing-key.asc \
  | sudo tee /etc/apt/keyrings/network-monitor.asc >/dev/null
sudo chmod 0644 /etc/apt/keyrings/network-monitor.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/network-monitor.asc] https://verdient.github.io/network-monitor stable main" \
  | sudo tee /etc/apt/sources.list.d/network-monitor.list

sudo apt update
sudo apt install network-monitor
```

安装后会自动启动 systemd 后台采集，后续可以通过 APT 更新。
软件源与签名公钥说明见 [APT 软件源](https://verdient.github.io/network-monitor/)。

### Linux：直接安装 deb

支持 Debian 11+、Ubuntu 20.04+ 的 amd64 和 arm64 系统。用 `dpkg --print-architecture` 查看架构，
下载对应的 deb 文件，然后在下载目录运行：

```bash
# amd64（常见 Intel / AMD 64 位机器）
sudo apt install ./network-monitor_0.2.0-1_amd64.deb

# arm64（64 位 ARM 机器）
sudo apt install ./network-monitor_0.2.0-1_arm64.deb
```

两条命令按架构选择一条即可，无需安装 Python。使用 systemd 的系统会自动启动采集并设置开机启动。
安装包已包含服务，使用 `systemctl` 管理即可。

## 开始采集

macOS 启动后台采集，并设置为登录后自动启动：

```bash
brew services start network-monitor
```

Linux 的 deb 安装完成后已启动后台采集；如果需要手动启动：

```bash
sudo systemctl start network-monitor
```

默认每秒采集一次，使用默认路由对应的网卡。首次启动后等待几秒，再查看统计：

```bash
network-monitor stats
```

## 查看统计

默认显示系统时区当天的数据，包括上传、下载、合计的平均速度、峰值、最低速度、累计流量和趋势图。
速度单位为 Mbps，流量以 B、KB、MB、GB 等显示。

### 选择网卡

默认查询默认路由对应的网卡。查询其他网卡时指定名称：

```bash
network-monitor stats -i en0
```

将 `en0` 换成实际网卡名，macOS 可用 `ifconfig -l` 查看，Linux 可用 `ip link show` 查看，例如 `eth0`、`ens3`。
同时采集多张网卡时，每次统计选择其中一张。

### 选择日期或时间范围

查询一天的数据：

```bash
network-monitor stats --start 2026-11-18 --end 2026-11-19
```

查询一天中的部分时段：

```bash
network-monitor stats --start '2026-11-18 09:00:00' --end '2026-11-18 18:00:00'
```

开始时间包含在范围内，结束时间不包含。也支持 Unix 秒时间戳。
查询历史数据时，请同时指定 `--start` 和 `--end`。

### 选择时区与显示方式

```bash
# 按北京时间查询当天的数据
network-monitor stats --tz Asia/Shanghai

# 按 UTC 查询指定日期
network-monitor stats --tz UTC --start 2026-11-18 --end 2026-11-19

# 只显示统计表
network-monitor stats --no-chart

# 调整趋势图的宽度和高度
network-monitor stats --width 80 --height 8
```

时区默认使用系统设置，也支持 `UTC+08:00` 等时差。未带时差的起止时间按所选时区解释。

## 配置采集网卡与采样间隔

Homebrew 安装的配置文件位于 `$(brew --prefix)/etc/network-monitor/config.ini`。
Linux deb 安装的配置文件位于 `/etc/network-monitor/config.ini`。

用编辑器打开它：

```bash
nano "$(brew --prefix)/etc/network-monitor/config.ini"
```

Linux 使用：

```bash
sudo nano /etc/network-monitor/config.ini
```

保留文件中已有的 `database` 路径，按需修改以下设置：

```ini
[monitor]
interface = en0, en1
interval = 1

[stats]
timezone = Asia/Shanghai
```

- `interface`：要持续采集的网卡，多张用逗号分隔；不设置时采集默认路由网卡。
- `interval`：采样间隔，单位秒，最小为 1。
- `timezone`：统计使用的时区；不设置时使用系统时区。
- `database`：数据保存路径。修改它后，新数据将写入新位置。

保存后重启服务使采集配置生效：

```bash
brew services restart network-monitor
```

Linux 使用 `sudo systemctl restart network-monitor`。如果修改数据库位置，需要让 `network-monitor` 服务用户对新目录具有读写权限。

也可以临时指定配置文件或覆盖参数：

```bash
network-monitor stats -c ./config.ini --tz UTC
```

支持的设置按 **命令行 > 配置文件 > 默认值** 生效。配置仅支持上面四项长期设置；
采集次数 `--count`、查询时间 `--start/--end`、图表和安装选项仅通过命令行指定。

## 临时采集

需要一次单独的采集时，使用独立数据库，避免与后台服务共用同一个采集任务：

```bash
# 每秒采集两张网卡，共 60 轮；也可随时按 Ctrl+C 停止
network-monitor monitor -i en0 en1 -d ./traffic.db --interval 1 --count 60

# 查看这次采集的数据
network-monitor stats -d ./traffic.db -i en0
```

省略 `--count` 会持续运行。相对数据库路径以执行命令时的目录为准。
同一个数据库可以同时查询，但只允许一个采集进程写入。

## 服务管理

macOS（Homebrew）：

```bash
brew services info network-monitor      # 查看状态
brew services restart network-monitor   # 重启
brew services stop network-monitor      # 停止并取消登录后启动
```

Linux（deb / systemd）：

```bash
systemctl status network-monitor              # 查看状态
sudo systemctl restart network-monitor        # 重启
sudo systemctl stop network-monitor           # 停止
sudo systemctl disable --now network-monitor  # 停止并取消开机启动
sudo systemctl enable --now network-monitor   # 启用开机启动并立即启动
journalctl -u network-monitor -f               # 查看日志，权限不足时加 sudo
```

## 数据与日志

| 内容 | macOS（Homebrew） | Linux（deb） |
| --- | --- | --- |
| 配置 | `$(brew --prefix)/etc/network-monitor/config.ini` | `/etc/network-monitor/config.ini` |
| 数据库 | `$(brew --prefix)/var/lib/network-monitor/network-monitor.db` | `/var/lib/network-monitor/network-monitor.db` |
| 日志 | `$(brew --prefix)/var/log/network-monitor/` | `journalctl -u network-monitor` |

修改配置中的 `database` 后，以配置的路径为准。
历史数据持续保留，不会自动清理。每张网卡每秒采样时，数据量参考值约为 4 MiB/天。

出现“暂无采样数据”时，先确认服务已启动，再检查网卡、数据库和查询日期是否正确。
统计默认只看当天，且不会自动查询配置中全部采集网卡。

## 更新与卸载

Homebrew 更新：

```bash
brew update
brew upgrade network-monitor
brew services restart network-monitor
```

Homebrew 卸载：

```bash
brew services stop network-monitor
brew uninstall network-monitor
```

卸载会保留配置和历史数据，便于重新安装后继续使用。

Linux 添加 APT 软件源后，更新方式如下：

```bash
sudo apt update
sudo apt install --only-upgrade network-monitor
```

直接安装 deb 的用户，也可以从 [Releases](https://github.com/Verdient/network-monitor/releases/latest) 下载新版本对应架构的 deb，
再运行 `sudo apt install ./下载的文件名.deb`。升级会保留配置和历史数据，并重启正在运行的采集服务；
已停止或禁用的服务不会被自动启动。

Linux 卸载：

```bash
sudo apt remove network-monitor
```

卸载会停止服务并保留配置、历史数据。使用 `sudo apt purge network-monitor` 可同时移除配置，历史数据仍会保留。

## 帮助

```bash
network-monitor -h
network-monitor monitor -h
network-monitor stats -h
```

问题与建议请提交到 [Issues](https://github.com/Verdient/network-monitor/issues)。
项目使用 [0BSD 许可证](LICENSE)。
