# network-monitor

网络流量采集和统计命令行工具，运行时仅依赖 Python 标准库，支持 Linux 和 macOS。

源码：https://github.com/Verdient/network-monitor 。项目代码采用 [0BSD](LICENSE)，允许商用、
修改和再分发，不要求署名或公开修改。二进制包中的第三方组件遵循各自许可证。

## Homebrew 安装

首次 Release 和 Tap 发布完成后，macOS 14+ 用户可以安装：

```bash
brew install Verdient/tap/network-monitor
network-monitor --version
brew services start network-monitor
network-monitor stats
```

支持 Apple Silicon 和 Intel。配置位于 `$(brew --prefix)/etc/network-monitor/config.ini`，
数据库位于 `$(brew --prefix)/var/lib/network-monitor/network-monitor.db`。
修改配置后运行 `brew services restart network-monitor`。
Homebrew 安装使用 `brew services` 管理，不执行程序自带的 `install`。
卸载程序不清理数据和用户配置。

维护者的构建、Release 草稿和 Tap 更新步骤见 [Homebrew 发布说明](packaging/homebrew/README.md)。

## 使用

源码运行需要 Python 3.9 或更新版本（使用标准库 `sqlite3` 和 `zoneinfo`）：

```bash
python3 network_monitor.py --help
python3 network_monitor.py monitor --help
python3 network_monitor.py stats --help
python3 network_monitor.py install --help

# 使用默认路由网卡，每秒采集一次，Ctrl+C 停止
python3 network_monitor.py monitor

# 同时采集多张网卡，采样间隔最小为 1 秒，count 表示轮数
python3 network_monitor.py monitor -i eth0 wlan0 -d network-monitor.db --interval 1 --count 60
# 也支持逗号分隔或重复指定 -i；重复名称自动去重
python3 network_monitor.py monitor -i eth0,wlan0
python3 network_monitor.py monitor -i eth0 -i wlan0

# 默认统计系统时区当天、默认路由网卡的数据
python3 network_monitor.py stats
python3 network_monitor.py stats -i eth0 --no-chart
python3 network_monitor.py stats -d network-monitor.db -i wlan0 --width 80 --height 8

# 选择查询时区和时间范围
python3 network_monitor.py stats -i eth0 --timezone Asia/Shanghai --start 2026-11-19 --end 2026-11-20
python3 network_monitor.py stats -i eth0 --tz UTC --start 2026-11-01T09:00:00 --end 2026-11-01T18:00:00
python3 network_monitor.py stats -i eth0 --start 1795046400 --end 1795132800
```

数据库默认是当前工作目录的 `network-monitor.db`，可用 `-d/--database` 指定。
采集自动创建父目录。同一个数据库只允许一个采集进程，但一个进程可同时采集多张网卡。

原有的 `python3 monitor.py ...` 和 `python3 stats.py ...` 也可继续使用。

## SQLite 分表与容量

每张网卡按 **Unix 日** 独立分表：`day = timestamp // 86400`，等价于 UTC 日。
这个分区规则仅做整数运算，采集过程不使用本地时区。改变统计时区不会改变分表。

| 1 秒采样，单张网卡 | 记录数 | 分表数 |
| --- | ---: | ---: |
| 每天 | 86,400 | 1 |
| 30 天 | 2,592,000 | 30 |
| 365 天 | 31,536,000 | 365 |

每条记录只保存 `timestamp`（整数 Unix 秒）、`duration_s`（实际采样时长）、
`upload_bytes` 和 `download_bytes`。速率在统计时计算，避免重复存储。
采样时长使用单调时钟测量，系统时钟调整不影响速率计算；相同 Unix 秒的记录允许共存，
不会因系统时间回拨而覆盖旧数据。

表名为 `samples_i<网卡名 UTF-8 十六进制>_d<Unix 日编号>`，例如 eth0 对应
`samples_i65746830_d20000`。`partitions` 表记录网卡与日分区的对应关系。
每个采样表有时间索引，查询先通过 `(interface, day)` 主键定位分区，再执行时间范围查询。
汇总和趋势图聚合都在 SQL 内完成，多天统计不会将全部采样加载进 Python 内存。

使用 86,400 条样本（实际时长 1.0001 秒、上下行增量 1.25 MB / 12.5 MB）实测，
数据库含时间索引约 **3.69 MiB/天/网卡，1.31 GiB/年/网卡**。
实际大小取决于字节数、采样时长等数值；多张网卡按数量近似线性增加，另有 WAL 临时空间。
分表控制单表规模，历史数据会持续保留，当前没有自动清理策略。

SQLite 使用 WAL，统计可以与采集同时进行。每轮多网卡写入使用同一事务；
统计使用一致的只读快照。单张网卡计数器重置时，只跳过该网卡本轮数据。

## 统计时间范围与时区

`stats` 默认只查询默认路由网卡，`-i/--interface` 可指定一张网卡。
它独立于采集的网卡列表，不会自动把多张网卡混在一起。

时区只用于统计：默认使用系统本地时区；`--timezone/--tz` 支持 IANA 时区名
（如 `Asia/Shanghai`、`America/New_York`）、`UTC`、`UTC+08:00` 或 `+08:00`。
负时差可以写成 `--tz UTC-05:00` 或 `--tz=-05:00`。
命名时区使用目标系统的时区数据库，并按所查询日期处理夏令时。

默认时间范围是所选时区的 **今天 00:00 到明天 00:00**；夏令时切换日可以是 23 或 25 小时。
`--start` 和 `--end` 支持 Unix 秒、`YYYY-MM-DD`、ISO 日期时间；不带时差的时间按
所选时区解释，带时差的时间按自身时差解释。
不存在的夏令时时间会报错；重复的本地时间可用明确时差消除歧义。

查询条件为采样结束时间 **`start <= timestamp < end`**，匹配的记录使用完整采样时长和流量，
不对边界采样做比例分摊。分别省略 `--start` 或 `--end` 时，各自使用今天或明天的零点；
查询历史区间建议同时指定两个参数。平均速率按实际采样时长加权；峰值为单次采样的平均速率。
趋势图按采样结束时间归入列，同样按时长加权。没有记录时显示“暂无采样数据”。

## 配置文件与参数优先级

配置文件面向长期服务设置，使用白名单：`[monitor] interface/database/interval`
和 `[stats] timezone`。这些设置遵循 **命令行参数 > 配置文件 > 内置默认值**。
配置可选，不提供时仍可直接运行；`-h` 显示当前平台实际的默认配置位置。

完整模板见 [network-monitor.example.ini](network-monitor.example.ini)：

```ini
[monitor]
# interface = eth0, wlan0
database = network-monitor.db
interval = 2

[stats]
# timezone = Asia/Shanghai
```

未指定 `-c` 时，按以下顺序查找，**只加载第一个存在的文件**：

1. 当前平台的用户配置位置。
2. 当前平台和架构的系统配置位置，详见下方路径表。

当前目录的文件需要用 `-c` 显式指定，避免服务配置受执行目录影响。

`-c/--config` 可放在子命令前后，显式指定文件时不会再加载其他配置：

```bash
python3 network_monitor.py -c ./custom.ini monitor
python3 network_monitor.py monitor --config ./custom.ini --interval 1
python3 network_monitor.py stats -c ./custom.ini --start 2026-11-19 --end 2026-11-20
```

第二条命令使用命令行的 `1` 秒覆盖配置中的 `2` 秒。
采集网卡在 `[monitor] interface` 中用逗号分隔；命令行的网卡列表整体覆盖配置列表。
统计时区可用 `[stats] timezone` 设置；统计网卡用命令行 `-i` 选择。
两个命令的数据库优先级都是：命令行 `--database` > `[monitor] database` > 内置路径。

`monitor --count`、`stats --start/--end`、图表选项和安装选项只支持命令行，
写进配置会报错。旧配置请移除 `count`、`start/end`、图表设置及整个 `[install]` 节。

旧 CSV 文件保留，本版本统计读取 SQLite；旧配置中的 `output` / `file` 请改为 `database`，
指向新的 `.db` 文件。旧 CSV 数据没有自动导入。

普通命令中的相对路径以当前工作目录为准，路径支持 `~`。
显式指定但不存在的配置、配置语法错误、未知配置节或配置项、非法参数都会报错。
`-h` 始终可用，不要求配置文件有效。

## 安装后台服务

`install` 自动识别操作系统：Linux 使用 **systemd**，macOS 使用 **launchd**。
它会检查程序是否在 PATH 中、选择配置位置、创建服务定义、启用并启动后台采集。
二进制不在 PATH 中时，会询问是否移动到默认二进制目录；已有配置不在默认位置时，
也会询问是否迁移。两个问题都以 **Yes 为默认值，直接回车同意**。
程序已经在 PATH 中时复用原路径；回答 No 时保留对应原路径，服务使用绝对路径启动。
所选配置会保存本次安装合并后的长期设置。

迁移先写入目标位置，服务启动成功后再移除原文件；启动失败会保留原文件。
目标目录尚未加入 PATH 时，安装完成会给出加入 PATH 的命令。

```bash
# 预览所有安装路径、配置与服务定义，不产生写入
./dist/network-monitor install -c ./custom.ini --dry-run

# 系统服务：需要 root，开机启动
sudo ./dist/network-monitor install -c ./custom.ini

# 当前用户服务：无需 sudo，登录后启动
./dist/network-monitor install --user -c ./custom.ini

# 安装时也可直接覆盖采集配置
./dist/network-monitor install --user -i en0 --interval 2 --work-dir ~/network-data

# 自动同意路径迁移，适合非交互安装
sudo ./dist/network-monitor install -c ./custom.ini --yes
```

后台服务始终持续运行，`install` 不提供 `--count`。
非交互安装请显式指定 `--yes`；输入流关闭不会被当作确认。
`--dry-run` 不提问、不写入，按默认同意展示安装结果。
Linux 需要可用的 systemd 服务管理器，用户服务还需要当前用户的 systemd 会话。
macOS 用户安装需要当前用户的图形登录会话。

| 文件或目录 | 系统安装 | 用户安装 |
| --- | --- | --- |
| Linux 程序（各架构） | `/usr/local/bin/network-monitor` | `~/.local/bin/network-monitor` |
| macOS ARM 程序 | `/opt/homebrew/bin/network-monitor` | `~/.local/bin/network-monitor` |
| macOS Intel 程序 | `/usr/local/bin/network-monitor` | `~/.local/bin/network-monitor` |
| Linux 配置 | `/etc/network-monitor/config.ini` | `$XDG_CONFIG_HOME/network-monitor/config.ini`，默认在 `~/.config` |
| macOS ARM 配置 | `/opt/homebrew/etc/network-monitor/config.ini` | `~/Library/Application Support/network-monitor/config.ini` |
| macOS Intel 配置 | `/usr/local/etc/network-monitor/config.ini` | `~/Library/Application Support/network-monitor/config.ini` |
| Linux 数据 | `/var/lib/network-monitor` | `$XDG_STATE_HOME/network-monitor`，默认在 `~/.local/state` |
| macOS 数据 | `/Library/Application Support/network-monitor` | `~/Library/Application Support/network-monitor` |
| systemd 定义 | `/etc/systemd/system/network-monitor.service` | `$XDG_CONFIG_HOME/systemd/user/network-monitor.service`，默认在 `~/.config` |
| launchd 定义 | `/Library/LaunchDaemons/io.network-monitor.plist` | `~/Library/LaunchAgents/io.network-monitor.plist` |

`--work-dir` 覆盖默认数据目录。安装时相对的 `database` 放在该目录下，父目录自动创建；
安装后的配置保存绝对数据库路径和完整网卡列表，服务不依赖原工作目录。
直接运行 `stats` 也可通过自动加载已安装的配置找到当天数据。
重复执行 `install` 会更新配置与程序，并重新启动服务。
将程序目录加入 `PATH` 后，即可直接使用 `network-monitor` 命令。

Linux 服务管理（用户安装时，在 `systemctl` / `journalctl` 后加 `--user`）：

```bash
sudo systemctl status network-monitor
sudo journalctl -u network-monitor -f
sudo systemctl restart network-monitor
sudo systemctl disable --now network-monitor
```

macOS 系统服务管理（用户服务将 `system` 换为 `gui/$(id -u)`，并省略 `sudo`）：

```bash
sudo launchctl print system/io.network-monitor
sudo launchctl kickstart -k system/io.network-monitor
sudo launchctl bootout system/io.network-monitor
```

macOS 日志位于数据目录下的 `service.log` 和 `service.error.log`。
修改安装后的配置文件后，需要重新启动服务；修改配置本身不需要重新安装。

源码运行也支持 `install`，会复制运行所需源码并创建启动脚本，需要本机 Python 持续可用。
二进制安装按上述迁移选择处理原文件。

## 打包成单个可执行文件

在项目目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python -m PyInstaller --clean --noconfirm network-monitor.spec
```

Debian/Ubuntu 如果提示缺少 `ensurepip`，请先安装与 Python 版本对应的
`python3-venv` 系统包，再创建虚拟环境。

产物为 `dist/network-monitor`，自带 Python 运行环境。可将该文件复制到目标机器，
无需安装 Python 或复制源码：

```bash
./dist/network-monitor --help
./dist/network-monitor monitor
./dist/network-monitor monitor -i eth0 --interval 1 --count 60
./dist/network-monitor stats
./dist/network-monitor stats -i eth0 --no-chart
```

把可执行文件放进 `PATH` 内的目录后，就可以直接运行 `network-monitor monitor`
和 `network-monitor stats`。相对路径始终以执行命令时的工作目录为准。

请在目标操作系统和 CPU 架构上分别打包。Linux 产物依赖目标系统的 glibc，
建议在计划支持的最旧 Linux 发行版上构建。Linux 自动选择网卡需要系统提供 `ip`
命令（通常由 `iproute2` 提供），也可以用 `--interface` 明确指定。
macOS 使用系统自带的 `route` 和 `netstat`。当前采集实现不支持 Windows。

## 验证

```bash
python3 -m unittest discover -s tests -v

# 对打包产物运行同一组集成测试
NETWORK_MONITOR_BINARY=dist/network-monitor python3 -m unittest discover -s tests -v
```

测试覆盖真实多网卡采集、SQLite 分区与时间索引、写入原子性、并发读写快照、
按网卡与日期查询、时区及夏令时、配置白名单与优先级、默认迁移确认、失败保留原文件、
平台架构路径、安装预览，以及 systemd/launchd
的文件生成和服务调用。服务管理器调用使用模拟，不会在测试期间安装实际后台服务。
