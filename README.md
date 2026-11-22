# network-monitor

记录网卡的上传、下载速度和累计流量，查看每天或指定时间段的平均网速、峰值与趋势图。
支持同时采集多张网卡，数据保存在本机，重启后可继续查询。

## 按操作系统下载

按操作系统和架构选择对应的安装包。[下载最新正式版](https://github.com/Verdient/network-monitor/releases/latest)

| 操作系统 | 架构 | 文件名结尾 |
| --- | --- | --- |
| macOS 14+ | Apple Silicon / Intel | `-macos-arm64.tar.gz` / `-macos-x86_64.tar.gz` |
| Linux（Debian 11+ / Ubuntu 20.04+） | amd64 / arm64 | `_amd64.deb` / `_arm64.deb` |

macOS 的 tar.gz 下载包解压后，运行 `./network-monitor monitor` 开始采集，运行 `./network-monitor stats` 查看统计。请把 `network-monitor-runtime` 目录与可执行文件保留在同一目录。
Linux 下载 deb 安装包后，按下面的步骤安装。
可使用发布页中同一版本的 `SHA256SUMS` 校验下载文件。

完整更新记录：[ChangeLog](https://github.com/Verdient/network-monitor/blob/main/CHANGELOG.md)。

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

支持 Debian 11+、Ubuntu 20.04+ 的 amd64 和 arm64 系统。用 `dpkg --print-architecture` 查看架构，再下载对应的安装包。[下载最新正式版](https://github.com/Verdient/network-monitor/releases/latest)

在下载目录运行以下命令，将示例文件名替换为实际下载的文件名。

```bash
sudo apt install './下载的文件名.deb'
```

安装包包含独立可执行文件和 systemd 服务。使用 systemd 的系统会自动启动采集并设置开机启动。

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

直接在终端运行 `network-monitor` 即可查看默认网卡的实时仪表盘，按 `q` 退出。`network-monitor -h` 列出实时选项和可用命令。运行 `network-monitor stats` 可查看一次历史统计，默认查询系统时区当天的数据。

在终端中，宽屏左侧显示下载、上传和合计，右侧显示同一时段的波形。实时采样时大号数字显示当前网速，查历史时显示累计流量。缩小窗口后，统计移到波形上方。流量占比按所查时段的累计字节数计算。

青色下载画在零线上方，粉色上传画在下方，两侧独立标尺，波形用不同色阶区分速率。箭头标出每个方向最高的分段平均速率，空间足够时还会显示该段的起止时间。零线上的琥珀色虚线 `┄` 表示该段缺少采样，实测速率为零时，波形贴着零线。

```bash
network-monitor --last 15m
```

实时仪表盘从 SQLite 读取打开之前的历史记录，随后每秒读取一次所选网卡。当前速率、累计流量和趋势图都会更新。即使没有启动后台采集或还没有数据库，等一个采样间隔就能看到第一个实时数据点。实时样本只保存在本次会话的内存中，退出后即清除。

实时趋势图固定左端起点，右端随当前时间推进，到达 `--end` 时停止延伸。每次刷新都会按图表宽度均分整个时间段，每个字符宽度可显示两个独立的数据点。上传和下载的纵轴上限分别取该方向图中最高速率的 1.25 倍。

默认从启动当天的零点开始。使用 `network-monitor --last 15m` 时，从启动前 15 分钟开始，之后持续显示从该起点累积到现在的数据。

实时模式默认每秒更新一次。点击底栏按钮或按对应的键即可操作。`+` 将刷新间隔延长 0.1 秒，`-` 将间隔缩短 0.1 秒，最短间隔为 0.1 秒。当前间隔显示在两个按钮之间，也可以在启动时指定。

```bash
network-monitor --last 15m --interval 0.5
```

拉高实时窗口会增加波形的绘图行数，快捷键始终位于底部。按 `c` 切到本机 TCP 连接，再按一次回到所选网卡的流量。切换视图期间采样照常进行。

连接页显示已建立、监听、TIME_WAIT 和其他状态的套接字数量。对端列表同时包含 IPv4 和 IPv6，按每个 IP 的已建立连接数排序。列表跨页时，按 `p` 上一页、`n` 下一页。连接数折线的纵轴上限取峰值的 1.25 倍并向上取整，图中标出峰值和采样时间。新采样从右侧进入，铺到左侧后按时间均分整个会话，每列保留该时段的最高连接数，最右列显示最新采样。Linux 汇总当前进程通过 `/proc` 能读取的网络命名空间，包括宿主机上可见的 Docker 容器。部分连接表读取失败时，连接页会标记「部分」。macOS 读取本机连接表。连接信息只保存在本次实时会话中。

窗口有足够空间且抓包成功时，实时主页在网速趋势下方显示 IP 流量表。按 `i` 可打开独立 IP 页，查看所选网卡上各对端 IPv4、IPv6 的下载速率、上传速率和双向累计流量。默认按当前双向速率之和排序。点击任一列表头按该列排序，再点一次切换升降序。点击 IP 流量框底部的「上页」或「下页」翻页，也可按 `p` 或 `n`。在独立页再按 `i` 返回。两处共用同一份采样，首次显示主页的 IP 表或打开独立 IP 页时开始计数，切换页面继续累计，退出时清除。流量按 IP 包长度计算，包含 IP 头。

IP 页使用 `tcpdump`，需要当前用户有权在所选网卡上抓包。macOS 自带该工具，Debian / Ubuntu 上可用 `sudo apt install tcpdump` 安装。IP、地域、运营商、AS 号和 DNS 反查域名按顺序显示，颜色逐级减淡。AS 号由地域数据源在同一请求中提供，例如 `AS45102`。地址栏空间不足时，附加信息放在下一行。

IPv4 私有网段、IPv6 唯一本地网段和链路本地地址直接标记为「局域网」。程序在后台通过 HTTPS 将当前页的公网对端 IP 发送给地域数据源，以查询地域和运营商。遇到限流、错误或信息不足时，按表中顺序尝试下一个来源。中国大陆 IP 有城市信息时显示为「杭州·浙江·中国」，缺少城市时显示为「浙江·中国」。直辖市和港澳台显示为「北京·中国」「香港·中国」「澳门·中国」「台湾·中国」等。美国 IP 显示为「山景城·加利福尼亚·美国」，缺少城市或州时逐级简化。其他国家显示中文国家名称。

中国大陆和美国的常见城市按省或州匹配本地别名。例如，`San Francisco` 和「三藩市」都显示为「旧金山」。地域和运营商的繁体中文通过内置 OpenCC 词典转成简体，已有缓存也会在读取时转换。[城市映射表](https://github.com/Verdient/network-monitor/blob/main/geo_data/cities.zh.json) · [OpenCC 1.1.9](https://github.com/BYVoid/OpenCC/tree/ver.1.1.9) · [OpenCC 许可证](https://github.com/Verdient/network-monitor/blob/main/geo_data/opencc/LICENSE)

| 数据源 | 程序限额（滚动 24 小时） | 最短间隔 | 说明 |
| --- | ---: | ---: | --- |
| ipwho.is | 900 次 | 1 秒 | [官方文档](https://ipwhois.io/documentation) |
| GeoJS | 250 次 | 2 秒 | [服务条款](https://www.geojs.io/tos/) |
| FreeIPAPI | 300 次 | 2 秒 | [官方限额](https://freeipapi.com/rate-limit) |
| IPQuery | 200 次 | 2 秒 | [官方文档](https://ipquery.io/developers/api) |
| IP2Location.io | 900 次 | 2 秒 | [官方文档](https://www.ip2location.io/ip2location-documentation) |
| Country.is | 150 次 | 2 秒 | 仅补充国家 [官方文档](https://country.is/) |

**Network Monitor uses IP2Location.io [IP geolocation](https://www.ip2location.io) web service.**

查询结果在本机缓存 30 天。各来源均查不到的结果缓存 24 小时，超时或限流按服务状态重试。缓存和请求记录共用当前用户的 `network-monitor/ip-location.sqlite3`，Linux 位于 `XDG_CACHE_HOME`（默认 `~/.cache`），macOS 位于 `~/Library/Caches`。收到 HTTP 429 时按 `Retry-After` 暂停对应来源，IP2Location.io 的无密钥额度用尽时暂停至下一个 UTC 零点。中文国家名称来自 Unicode CLDR 48，随程序附带许可证。[CLDR 48](https://github.com/unicode-org/cldr-json/tree/48.0.0) · [Unicode 许可证](https://github.com/Verdient/network-monitor/blob/main/geo_data/LICENSE.unicode)

按 `q` 退出。`--last` 支持秒 `s`、分钟 `m`、小时 `h` 和天 `d`，可单独使用。
使用 `network-monitor stats --plain` 可输出纯文本统计。`network-monitor stats` 重定向输出时也默认使用纯文本。设置 `NO_COLOR=1` 可保留仪表盘并关闭颜色。

仪表盘沿用终端背景，并尝试读取背景色来调整配色。读取不到时，文字沿用终端前景色，图表使用兼顾浅色和深色主题的配色。底栏按钮和 IP 表中可用的翻页按钮带底色。

### 选择网卡

默认查询默认路由对应的网卡。查询其他网卡时指定名称：

```bash
network-monitor stats -i en0
```

将 `en0` 换成实际网卡名，macOS 可用 `ifconfig -l` 查看，Linux 可用 `ip link show` 查看，例如 `eth0`、`ens3`。
同时采集多张网卡时，每次统计选择其中一张。

### 选择日期或时间范围

用 `-d` 或 `--day` 查询所选时区从当天零点到次日零点的数据。

```bash
network-monitor stats -d 2026-11-18
```

查询一天中的部分时段：

```bash
network-monitor stats --start '2026-11-18 09:00:00' --end '2026-11-18 18:00:00'
```

开始时间包含在范围内，结束时间不包含。也支持 Unix 秒时间戳。
查询其他历史时段时，请同时指定 `--start` 和 `--end`。

### 按周期汇总流量

`--traffic` 按周期显示下载、上传和合计流量。默认按天统计最近 15 个周期，包含当前周期。也可选 `hour`、`minute`、`second` 或 `month`，分别默认显示 24 小时、60 分钟、60 秒和 12 个月。用 `--count` 调整周期数量。

```bash
network-monitor stats --traffic
network-monitor stats --traffic hour --count 48
network-monitor stats --traffic hour --day 2026-11-18 --tz Asia/Shanghai
network-monitor stats --traffic month --end 2026-11-01
```

周期按所选时区划分。可用 `--day`、`--last` 或 `--start` / `--end` 限定汇总范围，表中最多显示该范围内最近的 `--count` 个周期。表格从第一个有采样的周期开始显示，之后缺少采样的周期用 `—` 标记。整个范围都没有采样时显示「暂无采样数据」。

### 选择时区与显示方式

```bash
# 按北京时间查询当天的数据
network-monitor stats --tz Asia/Shanghai

# 按 UTC 查询指定日期
network-monitor stats --tz UTC --start 2026-11-18 --end 2026-11-19

# 只显示统计指标，省略趋势图
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
network-monitor stats --database ./traffic.db -i en0
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

直接安装 deb 的用户可以下载新版本对应架构的安装包，再运行 `sudo apt install './下载的文件名.deb'`。升级会保留配置、历史数据和服务的启用状态，并重启正在运行的采集服务。[下载最新正式版](https://github.com/Verdient/network-monitor/releases/latest)

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
项目使用 [0BSD 许可证](https://github.com/Verdient/network-monitor/blob/main/LICENSE)。
