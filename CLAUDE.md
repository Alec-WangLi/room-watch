# room-watch · 新会话入口

先按顺序读：

1. `AGENTS.md` —— 项目约定、踩坑记录、命令、目录规则。
2. `docs/handoff/2026-10-19-session.md` —— 本轮共识、当前分支/版本、待办、环境。

必须遵守的几条：

- **分支**：`main` 是稳定版，`beta` 是测试版。新功能从 `beta` 切
  `feat/<主题>`，完成后先合回 `beta`，稳定后再合回 `main`；不要直接改 `main`。
- **优先级**：稳定性 > 性能 > 功能数量。最在意「一直活着、真出事一定通知到」。
- **版本**：只动第三位（0.2.x）；不替所有者宣布正式版；不擅自打 tag / 发 Release。
- **测试**：改代码先写失败测试；合并前跑全量 `rw test`（含集成测试）。
- **凭据**：只放 `config.local.yaml`，不打印、不提交、不贴进会话。
- **平台**：Windows 桌面版 + 本机 USB 摄像头；Linux 是以后的事，
  不要为它牺牲当前功能，也不要再加深 Windows 专属耦合。
- **解释器**：本机带 cv2 的是
  `C:\Users\Alec\.workbuddy\binaries\python\envs\default\Scripts\python.exe`；
  默认 `python` 可能没有 cv2，以 `rw doctor` 为准。

当前状态速览：`main` 与 `beta` 均为 0.2.0；已发 `v0.2.0` 稳定版。
本机仍是 0.1.24，所有者要求自己完成这次更新；更新入口见
`docs/handoff/2026-10-20-next-session.md`。
发版通道：稳定版从 `main` 走 plain Release，测试版从 `beta` 走 prerelease
（`scripts/release.py --channel stable|beta`）；发版需所有者明确同意。
