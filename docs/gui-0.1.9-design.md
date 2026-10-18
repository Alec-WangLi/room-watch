# 0.1.9 界面可信化改造（设计说明）

> 起因：0.1.7 实测反馈三个问题——启停卡死、系统音量被改、界面没有配置入口。
> 本文是这次改动的**唯一规格**；实现偏离本文即以本文为准。
> 读者：接手的人（或 AI）。所有定位用语义锚点（函数名/配置键），不写行号。

## 0. 一条共性结论

三个现象看着无关，根因是同一个：**界面在猜状态，而不是问状态。**

| 它在猜什么 | 猜错后的表现 |
|---|---|
| 「心跳 120 秒内 = 在线」 | 进程已被杀，界面仍显示在线 → 启动按钮禁用，最多卡 120 秒 |
| 「有人进来就要把主音量抬到保底值」 | 抬的是**系统主音量**，跟录音无关；强杀时还不还原 |
| 「预览图只要落在磁盘上就会被看到」 | probe 写 `var/media/preview.jpg`，界面只扫 `data/captures/` |

所以这次不修表面文案，而是把三处判定改成**可靠来源**：进程判活用 PID、
音量只作用于本进程音频会话、预览图与注册结果都回读真实产物。

## 1. 启停：从「猜心跳」到「问进程」

### 1.1 根因（有现场证据）

0.1.7 安装版 `var/logs/watcher.log` 与 `var/state/status.json`：

- `status.json` 记录 `pid 43396`、`last_frame_at 03:14:42`；
- 03:24 检查时 `tasklist` 里已无 43396，只剩一个 gallery 进程；
- `gui.read_state()` 只按 `last_frame_at` 距现在 ≤ `STALE_SECONDS(120)` 判在线。

结论：`taskkill /F` 之后，最长 120 秒内界面仍认为「在线」，
`btn_start` 因此 `state="disabled"`，用户表现为「停止只停到一半，再点不了启动」。

### 1.2 方案

1. **判活改为 PID 存活 + 心跳新鲜**，两者同时成立才算在线。
   `procutil.pid_alive()` 用 `psutil`（已依赖）优先，退回 `tasklist`；
   `updater._pid_alive` 改为委托它，避免两套判定漂移。
2. **停止改为两段式**：
   - 第一段「优雅停止」：界面写 `var/state/stop.request`（JSON：pid + 时间），
     watcher 主循环每 0.5 秒查一次，命中就 `break`，
     走既有 `finally`（录像收尾 → 音轨合并 → 状态落盘 → 音量还原）；
   - 第二段「兜底强杀」：20 秒内进程还没退，弹窗询问后才 `taskkill /F`。
3. **界面有明确的 stopping 相位**：按下停止后两个按钮都禁用、状态显示
   「正在停止…（录像收尾中）」，不给人「再点一次」的机会；进程一消失立刻刷新。
4. 启动前先 `control.clear_stop_request()`，避免上一轮残留的请求把新进程秒杀。
5. `rw stop` 与界面走同一条代码路径（`control.stop_watcher`），命令行也能优雅停。

### 1.3 验收

- 停止后 ≤1 秒「启动监控」可用（不必等心跳过期）。
- 录像进行中点停止：mp4 能被相册播放（收尾完成），日志出现「收到停止请求，正在收尾」。
- 进程被外部强杀：界面在 2 秒内显示「已停止」，启动按钮可用。

## 2. 音量：录音与播报彻底分开

### 2.1 根因（有现场证据）

日志：`03:11:07 系统音量：主音量 2.0% → 70.0%，退出时还原`。

- 录音走**麦克风输入**（`audio_recorder.py`），与系统播放音量无关；
- 抬主音量只是为了 `speaker.py` 的 TTS 播报能被听见；
- `taskkill /F` 不触发 `VolumeGuard.restore()`，音量就永久停在 70%。

### 2.2 方案

新增 `voice.volume_mode`，三档：

| 值 | 行为 | 是否动系统主音量 |
|---|---|---|
| `off`（**新默认**） | 完全不碰 | 否 |
| `session` | 只把本进程在音量合成器里的那条拉到 100% | 否 |
| `master` | 旧行为：主音量保底到 `ensure_system_volume` | 是（界面里明确警示） |

- 兼容判定 `resolve_mode()`：有 `volume_mode` 就用它；没有且
  `ensure_system_volume > 0` 视为 `master`（老配置不改变行为）。
- `config.yaml` 默认改为 `volume_mode: off` + `ensure_system_volume: 0`，
  安装包升级会覆盖 `config.yaml`，用户侧即刻生效。
- 即便选了 `master`，也补一道崩溃兜底：改动前把原值写
  `var/state/volume_backup.json`（含 pid）；下次启动发现「记录里的进程已不在」
  就先还原再按当前策略处理。`off`/`session` 模式不写这个文件。

### 2.3 验收

- `volume_mode: off` 时，启动监控前后主音量百分比不变（日志写明「未启用（不碰系统音量）」）。
- `session` 模式下主音量滑块不变，播报仍可听清（本程序音轨 100%）。
- `master` 模式强杀后重启：日志先出现「上次异常退出，已还原主音量」。

## 3. 界面配置：四项常驻设置

### 3.1 为什么写 `config.local.yaml` 而不是 `config.yaml`

安装包的 `[Files]` 是 `Source: {#SourceDir}\*` + `ignoreversion`，
**升级会整体覆盖 `config.yaml`**。界面若把用户设置写进主配置，
下次更新就静默丢失。所以界面写的一律落 `config.local.yaml`（深合并、升级保留），
与 `rw push` / `rw setup` 一致。

### 3.2 页签与键

| 页签 | 配置键 | 说明 |
|---|---|---|
| 推送 | `notify.channel`、`notify.wecom_bot.webhook`、`notify.wecom_bot.send_video`、`notify.wxpusher.app_token`、`notify.wxpusher.uid`、`notify.serverchan.sendkey` | 含「发送测试消息」 |
| 身份 | `identity.threshold`、`identity.unknown_policy`、`identity.on_owner` | 显示模板状态（样本数/注册时间）；可注册、用最新抓拍注册、清除 |
| 自启 | 注册表 `HKCU\...\Run\RoomWatch` | 状态回读；注册/取消；失败给手工命令 |
| 声音 | `voice.enabled`、`voice.text`、`voice.cooldown_seconds`、`voice.volume_mode` | 三档音量策略 |

### 3.3 硬约束

1. **界面进程不 import yaml / cv2**（打包后只有 tkinter + Pillow）。
   读写配置、探测摄像头、注册人脸，全部 `subprocess` 调 `rw` 子命令，
   子进程一律 `PYTHONIOENCODING=utf-8` 且捕获输出。
2. **密钥永不回显全文**：`rw config --json` 只返回 `{set: bool, tail: "…末4位"}`，
   界面上是只读掩码 + 「粘贴新值」输入框；留空表示不修改。
3. **写完复读校验**：`settings.apply()` 落盘后用 `load_config()` 重新读一遍，
   逐个比对生效值；不一致就报错（不静默成功）。
4. 值类型在 `settings.SPEC` 里声明并校验范围（阈值 0.1~0.99 等），
   不把字符串塞进布尔位。
5. 自启写注册表用 `winreg`，不调 `reg.exe`（本机策略与沙箱都会拦）。

### 3.4 验收

- 改 webhook → 保存 → 关掉重开界面仍是新值，且 `config.local.yaml` 里能查到。
- 输入错误 webhook 点「测试」→ 明确失败原因，不写坏配置。
- 注册本人后主窗口出现「本人模板：已注册（N 个样本 · 时间）」，
  未注册时是「未注册（所有脸按访客处理）」。
- 自启开→关→开，注册表值随之出现/消失；重开界面状态一致。

## 4. 预览图与注册结果：回读真实产物

1. `probe` 增加 `--json`：输出一行
   `{"ok":true,"path":"...preview.jpg","width":1280,"height":720,"ms":812,"error":""}`，
   失败也输出 JSON 并带 `error`（不再只有退出码）。
2. 界面缩略图取「`data/captures/**/*.jpg` 里最新」与「`var/media/preview.jpg`」
   两者中 mtime 更新的那个，并在标题里标明来源与时间。
3. 注册本人：捕获子进程输出，完成后弹结果（成功给样本数，失败给最后几行原因），
   主窗口常驻显示模板状态；无摄像头时可「用最新抓拍注册」（`enroll --from`）。

## 5. 测试清单（全部可离线）

| 文件 | 覆盖 |
|---|---|
| `tests/test_procutil.py` | 自身 PID 存活、不存在 PID、wait 超时/立即返回 |
| `tests/test_control.py` | stop.request 读写/清理、pid 不匹配不算、优雅→强杀顺序、超时 |
| `tests/test_settings.py` | 类型校验、范围、密钥写 local 且掩码、复读一致、保留已有键 |
| `tests/test_autostart.py` | vbs 内容、注册值拼装、启用/取消/状态、非 Windows 降级 |
| `tests/test_gui_state.py`（扩） | PID 判活、stopping 相位、预览图优先、按钮可用性矩阵 |
| `tests/test_volume_mode.py` | resolve_mode 兼容矩阵、备份还原纯函数 |

## 6. 版本

`0.1.8` 是上一轮的修订号，本次 → `0.1.9`。前两位 `0.1` 不动（版本纪律）。
