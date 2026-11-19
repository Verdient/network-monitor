# Homebrew 首次发布

源码与 Releases：https://github.com/Verdient/network-monitor

Tap：https://github.com/Verdient/homebrew-tap

首版 `v0.1.0` 已发布，正式配方位于 Tap 的 `Formula/network-monitor.rb`。
以下流程用于后续版本维护。

1. 源码推送到 `main` 后，确认 CI 在 Linux、macOS ARM64 和 Intel 上通过。
2. `version.py` 中首版版本为 `0.1.0`。创建并推送 `v0.1.0` 标签。
3. `Release` 工作流会分别构建两个 macOS 二进制，测试源码与可执行文件，生成归档，
   并在两个架构上执行真正的 `brew install` / `brew test`。
4. 全部通过后，工作流创建 **Draft Release**，附带两个归档、`SHA256SUMS` 和
   已填入实际 SHA-256 的 `network-monitor.rb`。构建 runner 使用 macOS 15，支持范围先定为 macOS 14+。
5. 检查草稿后发布 Release，再下载其 `network-monitor.rb`，放入 Tap 仓库的
   `Formula/network-monitor.rb` 并提交。Formula 的 URL、版本和校验和由脚本生成，不手工猜测。
6. 在 macOS 上验证：

   ```bash
   brew install Verdient/tap/network-monitor
   brew test Verdient/tap/network-monitor
   network-monitor --version
   brew services start network-monitor
   brew services info network-monitor
   network-monitor stats
   ```

配置保存在 `$(brew --prefix)/etc/network-monitor/config.ini`，Homebrew 在升级时保留用户修改。
数据库位于 `$(brew --prefix)/var/lib/network-monitor/network-monitor.db`，升级不会删除数据。
程序通过安装标记识别 Homebrew prefix，兼容 Apple Silicon、Intel 和自定义 prefix。
Homebrew 安装的服务由 `brew services` 管理，程序的 `install` 会提示正确命令。

Tap 仓库只需要 `Formula/network-monitor.rb`、`README.md` 和相同的 `LICENSE`。
首次发布手动提交配方即可，不需要跨仓库访问令牌；后续可再自动化更新 Tap。

后续发布先修改 `version.py`，推送相应标签，再重复草稿审核与配方更新。
