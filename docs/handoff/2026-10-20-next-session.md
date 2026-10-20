# 下一轮交接（2026-10-20 晚 · 0.1.22）

> 新会话先读 `CLAUDE.md` → `AGENTS.md` → 本文；0.1.21 细节在
> `docs/handoff/2026-10-19-session.md`（第 18 节）。

## 1. 当前状态

- `main` = `b63f8a2`，稳定版 0.1.12，未动。
- `beta` = `4b801d7`（0.1.22），已推送。
- 本轮分支 `feat/0.1.22-ui-fixes` 已合回 `beta`。
- 已发布 `v0.1.22-beta`（prerelease，tag 指向 `4b801d7`）：
  https://github.com/Alec-WangLi/room-watch/releases/tag/v0.1.22-beta
  资产 `room-watch-0.1.22-setup.exe` + `.sha256`；简介取自 CHANGELOG。
- 本机安装：`%LocalAppData%\Programs\room-watch`，`VERSION` 与卸载项均为
  0.1.22；更新通道 beta；监控、guardian、gallery 正常运行。
  **注意**：0.1.22 安装器最后是从终端面板（真实用户会话）重跑了一遍，
  因为 Agent 的 Bash 工具写注册表落在隔离命名空间（见第 5 节）；真实注册表
  已用用户 PowerShell 的 `reg query` 和 `Get-Package` 复核为 0.1.22。
- 安装包：`dist\room-watch-0.1.22-setup.exe`（143.1 MB）、
  `dist\room-watch-0.1.22.zip`（177.1 MB）。

## 2. 0.1.22 做了什么

- **更新通道**：检查结果带代次，保存 `update.*` 后旧结果作废并立即重查；
  检查结果缺通道标签时不再默认「稳定版」；保存期间的手动检查排队重跑。
- **版面**：主界面底栏优先占底部、按实际宽度换行、项目/解释器分两行；
  设置页所有说明文字按实际宽度换行；设置窗口最小 720×680，底部按钮优先占位。
- **保存提示**：reload 不再覆盖「已保存…」。
- **构建自检**：九个设置页 × 800×700 / 720×680 两档尺寸，检查说明文字、
  按钮/输入框和内容高度；主界面检查底栏与事件表；`build-exe.ps1` 自检失败即停。
- **真机新发现并修复**：
  - `rw start` 的常驻 guardian 与 `guard --once` 双起 watcher（两个实例抢摄像头）。
    拿到检查锁后先 `_reload_state()`，已加回归测试。
  - 更新安装器返回 5（RestartManager Abort）后界面静默重开、卸载项停在旧版本。
    交接脚本改为等待更新器 PID 退出；失败写 `var/state/update_failed.txt`，
    界面下次启动提示「上次更新没有完成」；安装器替换文件前再杀一遍进程。

## 3. 真机验证记录

- `rw start` 后只有一个 watcher（当前 PID 随重启变化）与一个 guardian；gallery 正常。
- `room-watch.exe --selftest`：`ok 0.1.22 … 九个设置页两档尺寸无裁切`。
- 更新检查：`{"channel": "beta", "channel_label": "测试版"}`；界面手动检查弹窗
  「当前版本 0.1.22 就是最新版本（测试版通道）」。
- 保存更新设置后状态栏保持「已保存并立即生效。」，旧检查代次作废。
- `ui-check/shoot.py` 与 `ui-check/check_flow.py` 可复跑（截图在 `ui-check/shots/`）。

## 4. 下一步

1. 观察 0.1.22 真机运行与后续自动更新；稳定后再把 `beta` 合 `main`，
   从 `main` 发 stable（同版本号不要重复发，先升修订号）。
2. 未做：托盘（pystray + 真机）、M6 平台抽象、干净机器安装验证。
3. 若自动更新再次失败：先看 `var/logs/update.log` 的
   `installer rc=` 行，再看 `var/state/update_failed.txt`。

## 5. 环境与命令

- 带 cv2 的解释器：
  `C:\Users\Alec\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 全量测试：`<解释器> -m roomwatch test`（`--fast` 跳过集成）。
- 打包：`scripts\build-exe.ps1` → `build-portable.ps1` → `build-installer.ps1`
  （Inno：`%LocalAppData%\Programs\Inno Setup 6\ISCC.exe`）。
- **Git Bash 里不要用 `/VERYSILENT` 直接跑安装包**（会被路径转换吃掉）；
  用 PowerShell `Start-Process -ArgumentList` 传参。
- **Agent 的 Bash 工具在隔离注册表命名空间**：文件写入共享，HKCU 写入不落
  用户会话。安装器/更新器必须从**终端面板**（真实会话）跑；装完在真实
  PowerShell 里用 `reg query` 或 `Get-Package` 复核卸载项版本。
  `dangerouslyDisableSandbox` 不改变这一点。
- 发版：`scripts/release.py --channel beta|stable`；简介自动取 CHANGELOG。
- 凭据只用 `git credential fill` 或 `GH_TOKEN`，不要打印。

## 6. 规则提醒

- 稳定性 > 性能 > 功能数量；真出事一定通知到。
- 版本只动第三位；先写失败测试；不擅自打 tag / 发 Release。
- 所有者说「推送到远程」默认等于打 tag + 发 Release；通道要判断正确。
- 不要杀正在运行的 watcher/gallery；界面窗口可以关。

## 7. 0.1.23 / 0.1.24（更新链路）

- 0.1.23 已合 `beta` 并发 `v0.1.23-beta`（tag `96328cb`）：gallery `/healthz`
  + guardian 假活重启、`rw doctor` 安装版本检查、安装器静默模式恢复监控。
- **自动更新真机测试发现 0.1.22/0.1.23 的交接脚本会卡死**：helper 用
  `tasklist /FI ... | find ...` 等更新器 PID，在无控制台的分离 cmd 里
  `find` 一直等管道 EOF，安装器永远不启动。已改为 tasklist 先写临时文件
  再 `find`，读不到也立刻放行。
- 0.1.24 已合 `beta`、推送并发 `v0.1.24-beta`（tag `d852c2c`）：
  https://github.com/Alec-WangLi/room-watch/releases/tag/v0.1.24-beta
  资产 `room-watch-0.1.24-setup.exe` + `.sha256`；简介取 CHANGELOG。
- 真机复验：用本地安装包走真实交接脚本，`installer rc=0`、监控自动恢复
  （新 watcher PID）、界面自动重开 0.1.24、无 `update_failed.txt`；
  真实注册表/`Get-Package`/`rw doctor` 一致；更新检查返回
  「0.1.24 / 测试版 / 已是最新」。
- **0.1.22/0.1.23 已装用户需手动安装一次 0.1.24**；之后的自更新使用修复后的
  交接脚本。0.1.23 的 Release 按所有者要求保留。
- 慢速网络下载没有完整跑完（3 分钟约 8MB）；检查/下载逻辑已实际触发，
  交接/安装/恢复用本地安装包验证。

## 8. 0.2.0 稳定版（2026-10-20 深夜）

- **版本基线经所有者批准升到 0.2.0**：`VERSION` = 0.2.0，
  `VERSION-APPROVED` = 0.2（所有者原话：「你要发稳定版的话，就改版本号为 0.2.0」）。
- **发布**：`v0.2.0` 稳定版（tag `bc3192c`，普通 Release）：
  https://github.com/Alec-WangLi/room-watch/releases/tag/v0.2.0
  资产 `room-watch-0.2.0-setup.exe`（143.1MB）+ `.sha256`；
  sha256 `0cbe2f1d14ad7fec996521b1e460348fb0f4754866a849120ba3987252ee34ed`。
  本机 `dist\room-watch-0.2.0-setup.exe` 与上传资产同源。
- **内容**：0.1.13–0.1.24 的稳定化改动合入 `main`；测试代码不再带真实
  企业微信 webhook。
- **企微机器人已轮换（2026-10-20 23:2x）**：所有者在企微后台重建机器人，
  新 webhook 已写入 `config.local.yaml`（非旧 key）；`rw config --test`
  返回「wecom_bot：测试消息已发出」。通知类设置保存后需重启 watcher+guardian
  （热更新只覆盖语音），本次已重启（watcher PID 41292，23:19 启动）；
  gallery 已恢复（PID 40228，`/healthz` ok）。
- **测试夹具清理**：`tests/test_settings.py` 的真实 webhook 已换成假值；
  旧 key 仍在 git 历史中，但机器人已作废，暴露不再有效。
- **测试与构建**：`beta` 与合并后的 `main` 全量 `rw test` 通过；
  `room-watch.exe --selftest` 通过（九页两档尺寸）；便携包 `rw doctor` 全过；
  安装包 143.1MB。
- **自动更新实测（0.1.24 → 0.2.0）**：GUI 显示「有新版本 0.2.0」；
  `rw update --check` 返回 `confirm-minor`；所有者自行完成下载与安装，
  `update.log` 最后一条 `installer rc=0`，无 `update_failed.txt`。
- **安装后验证**：`VERSION`/`rw version`/卸载项/`rw doctor` 均为 0.2.0；
  watcher、guardian、gallery 各一个，摄像头画面正常，`/healthz` 返回
  `{"ok": true}`；本人模板与存储数据保留。
- **网络注意**：GitHub Release 边缘速度随机。首连只有 5–12 KB/s（143MB
  要数小时），重开连接后达到 629 KB/s（约 4 分钟下完）。GUI 更新若长时间
  停在低速，取消后重试即可；不要改 updater 的重试逻辑。
- **待办**：CI 启用；干净机器验证；托盘；M6。

## 9. 0.2.1 资源优化（已就绪，2026-10-21 凌晨）

- `beta` 已合并 `6d1d60d`（特性提交 `76083e5`）；未推送、未打 tag、未发版。
- 内容：本人在场、画面里有人且未录像时，推理降到每秒 2 次
  （`gate.owner_interval_seconds`，默认 0.5，0 = 关闭）；录像中、身份未确认、
  画面空了仍全速。背景数据：真机 0.2.0 空闲 10 分钟 5749 帧全部推理，
  进程约 1.3 核，原因是门控的 busy 被本人每一帧续期。
- 实现：`rules.OwnerInferenceThrottle` + watcher 主循环；降频期间沿用
  上一次检测框，规则层不会误判「人走了」；状态里 `gated.owner_throttled`
  可直接观察被跳过的帧数。
- 测试：`test_logic.py::TestOwnerInferenceThrottle`（6 项）+
  `test_integration.py::test_owner_present_throttles_inference`；
  全量 `rw test` 通过（2026-10-20 与 21 日各跑一次）。
- 构建：`dist\room-watch-0.2.1-setup.exe`（143.1MB，sha256
  `ea4e416166348d923c34f5fad497240f21ebe83b6341c459c3395841c115cf2a`）、
  `dist\room-watch-0.2.1.zip`（177.1MB）。
- 真机验证（2026-10-21 00:07 起）：安装版 `VERSION`/卸载项/`rw doctor`
  均为 0.2.1；界面 `--selftest` 通过；一个 watcher + 一个 guardian +
  一个 gallery；相机正常、`/healthz` ok、无 `update_failed.txt`。
  同场景进程 CPU 从约 128% 降到 52–55%；3 分钟 1748 帧里推理 353 次，
  `owner_throttled` 1395。
- 待办：推送与发版需所有者明确同意；稳定观察后再把 `beta` 合回 `main`。
