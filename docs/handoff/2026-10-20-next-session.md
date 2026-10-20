# 下一轮交接（2026-10-20 晚）

> 新会话先读 `CLAUDE.md` → `AGENTS.md` → 本文；上一轮细节在
> `docs/handoff/2026-10-19-session.md`（第 18 节为 0.1.21）。

## 1. 当前状态

- `main` = `b63f8a2`，稳定版 0.1.12，未动。
- `beta` = `af23c84`，**0.1.21 测试版**，已推送。
- 已发布 `v0.1.21-beta`（prerelease，非 draft）：
  https://github.com/Alec-WangLi/room-watch/releases/tag/v0.1.21-beta
  资产 `room-watch-0.1.21-setup.exe` + `.sha256`；简介取自 CHANGELOG。
- 本机：0.1.20 正在升级到 0.1.21；更新通道已保存为 `beta`，
  更新器实测 stable → v0.1.10、beta → v0.1.21-beta。
- 本地安装包：`dist/room-watch-0.1.21-setup.exe`。

## 2. 0.1.21 做了什么

- 语音只在身份等待窗口给出结论后响：本人撤销不响，确认陌生人/窗口到期才响。
- 更新 / 自启 / 声音三页保存后热更新（`reload.watcher` / `reload.guardian`）；
  设置逐项标注生效方式；不再一律提示重启监控。
- 主界面显示「已运行 1h00m」与「启动 17:16」。
- 企业微信 Webhook 明文显示在输入框；其他凭据仍掩码。
- 回滚点：0.1.20 的更新通道与 15 秒身份等待窗口仍有效。

## 3. 下一轮优先修（0.1.22）

1. **保存提示被覆盖**：`roomwatch/gui_settings.py` 的 `_on_saved()` 先设置
   “已保存…”，随后 `self.reload()` 的 `_on_snapshot()` 又把状态改回
   “已读取…”，用户以为没保存。修法：保存后保留一条 `_status_after_reload`
   或先 reload 再设置状态；加测试。
2. **检查更新与保存的竞态**：`roomwatch/gui.py` 的 `_start_check()` 启动
   `rw update --json` 子进程，子进程在启动时读取配置；若用户恰好在保存
   `update.channel` 之前/同时点检查，结果仍是旧通道（真机出现过
   “已保存 beta 但仍显示稳定版通道”）。修法建议：
   - 给检查加 generation 计数，`_poll_check` 丢弃旧 generation 的结果；
   - 保存 `update.*` 成功后作废旧结果并立即按新配置重查一次；
   - 或设置窗口保存期间禁用主界面检查。
3. （可选）设置窗口改成模态，避免“设置还开着、主界面又在检查”。

## 4. 再往后

- 真机验收 0.1.21：启动不再自动念语音；侧脸→正脸不响不推；三页设置立即生效；
  运行时长显示；Webhook 输入框可见完整地址。
- 托盘：需要 `pystray` + 真机确认，未做。
- M6 平台抽象：大重构，需真机回归，未做。
- 稳定后：先把 `beta` 升修订号再合 `main`，从 `main` 发 stable Release；
  同版本号会让已装 beta 的机器不再升级。

## 5. 环境与命令

- 带 cv2 的解释器：
  `C:\Users\Alec\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 全量测试：`<解释器> -m roomwatch test`（`--fast` 跳过集成）。
- 打包：`scripts\build-exe.ps1` → `build-portable.ps1` → `build-installer.ps1`
  （Inno：`%LocalAppData%\Programs\Inno Setup 6\ISCC.exe`）。
- 发版：`scripts/release.py --channel beta|stable`；简介自动取 CHANGELOG；
  跨版本跳发用 `--notes-include`，补写历史用 `--notes-only`。
- 凭据只用 `git credential fill` 或 `GH_TOKEN`，不要打印。
- 不要杀正在运行的 watcher/gallery（界面窗口可以关，监控在后台）。

## 6. 规则提醒

- 稳定性 > 性能 > 功能数量；真出事一定通知到。
- 新功能从 `beta` 切 `feat/<主题>`，合并前跑全量测试。
- 版本只动第三位；先写失败测试；不擅自打 tag / 发 Release。
- 所有者说「推送到远程」默认等于打 tag + 发 Release；通道要判断正确。
