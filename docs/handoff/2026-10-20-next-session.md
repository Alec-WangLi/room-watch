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
