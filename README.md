# network-monitor

记录网卡的上传、下载速度和累计流量，查看每天或指定时间段的平均网速、峰值与趋势图。
支持同时采集多张网卡，数据保存在本机，重启后可继续查询。

## 安装

### macOS

需要 macOS 14 或更新版本，支持 Apple Silicon 和 Intel。先安装 [Homebrew](https://brew.sh/)，然后运行：

```bash
brew install Verdient/tap/network-monitor
```

确认安装成功：

```bash
network-monitor --version
```

### Linux

当前提供源码安装，需要 Git、Python 3.9 或更新版本，以及 systemd。程序无需安装额外的 Python 包。
自动识别网卡需要系统提供 `ip` 命令，通常由 `iproute2` 包提供。

```bash
git clone https://github.com/Verdient/network-monitor.git
cd network-monitor
python3 network_monitor.py install --user
export PATH="$HOME/.local/bin:$PATH"
```

安装完成后会自动启动当前用户的后台采集服务，登录后启动。将上面的 `export PATH` 加入
`~/.bashrc` 或所用 Shell 的配置文件，可在新终端中直接运行 `network-monitor`。

如果希望开机就采集，将上面的 `install --user` 换成系统服务安装：

```bash
sudo python3 network_monitor.py install
```

安装会显示程序、配置和数据的保存位置，迁移提示直接回车即同意。
不使用 systemd 时，也可以在项目目录运行 `python3 network_monitor.py monitor`，按 `Ctrl+C` 停止。

## 开始采集

macOS 使用 Homebrew 启动后台采集：

```bash
brew services start network-monitor
```

默认每秒采集一次，使用默认路由对应的网卡。首次启动后等待几秒，再查看统计：

```bash
network-monitor stats
```

Linux 按上面的步骤安装后已经开始采集，可以直接运行 `network-monitor stats`。

## 查看统计

默认显示系统时区当天的数据，包括上传、下载、合计的平均速度、峰值、最低速度、累计流量和趋势图。
速度单位为 Mbps，流量以 B、KB、MB、GB 等显示。

### 选择网卡

默认查询默认路由对应的网卡。查询其他网卡时指定名称：

```bash
network-monitor stats -i en0
```

将 `en0` 换成实际网卡名，例如 Linux 上的 `eth0`、`ens3`。
macOS 可用 `ifconfig -l` 查看网卡；Linux 可用 `ip link show`。
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

用编辑器打开它：

```bash
nano "$(brew --prefix)/etc/network-monitor/config.ini"
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

Linux 用户服务默认配置位于 `~/.config/network-monitor/config.ini`，系统服务位于
`/etc/network-monitor/config.ini`；设置了 `XDG_CONFIG_HOME` 时，用户配置使用该目录。
修改后分别用 `systemctl --user restart network-monitor` 或 `sudo systemctl restart network-monitor` 重启。

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

### Homebrew

```bash
brew services info network-monitor      # 查看状态
brew services restart network-monitor   # 重启
brew services stop network-monitor      # 停止并取消登录后启动
```

Homebrew 安装后使用 `brew services` 管理服务。

### Linux systemd

当前用户服务：

```bash
systemctl --user status network-monitor
systemctl --user restart network-monitor
systemctl --user disable --now network-monitor
journalctl --user -u network-monitor -f
```

系统服务使用相同命令时省略 `--user`，并在前面加 `sudo`。

## 数据与日志

| 安装方式 | 数据库 | 日志 |
| --- | --- | --- |
| Homebrew | `$(brew --prefix)/var/lib/network-monitor/network-monitor.db` | `$(brew --prefix)/var/log/network-monitor/` |
| Linux 用户服务 | `~/.local/state/network-monitor/network-monitor.db` | `journalctl --user -u network-monitor` |
| Linux 系统服务 | `/var/lib/network-monitor/network-monitor.db` | `sudo journalctl -u network-monitor` |

自定义数据库、数据目录或 `XDG_STATE_HOME` 后，以安装输出和配置文件为准。
历史数据持续保留，目前没有自动清理功能。每张网卡每秒采样时，数据量参考值约为 4 MiB/天。

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

## 帮助

```bash
network-monitor -h
network-monitor monitor -h
network-monitor stats -h
network-monitor install -h
```

问题与建议请提交到 [Issues](https://github.com/Verdient/network-monitor/issues)。
项目使用 [0BSD 许可证](LICENSE)。
