# room-watch · 项目约定与协作习惯

> 读者：未来的 AI 助手，以及用户本人。
> 目标：开新对话时先读这一页，就能直接干活，不用重新摸一遍。
> 本文所有命令、路径、配置键名在 **2026-10-18 重构（v0.1.0）时逐条核对过源码**。
>
> **关于引用**：一律用**语义锚点**（配置键名、函数名、段名、命令名）定位，
> 不写行号。行号会随编辑漂移，曾导致整段引用失效；写「在 `cli.py` 的 `COMMANDS` 表里」
> 比写「第 96 行」可靠得多。

---

## A. 项目定位与数据流

**一句话**：摄像头盯着房间，YOLO 确认「有人进来了」就抓一张带时间戳的照片、
同时录一段 mp4、本机语音播报、再推一条微信消息。全部在本机跑，视频不出机器。

### 主链路（括号里是出处，用键名/函数名定位）

```
采集源        camera.source：本机编号 / rtsp:// / 视频文件
              设备走 dshow，网络流走 FFmpeg（TCP + 5s 超时）        （camera.py）
   ↓
静止门控      empty room → 帧差不过阈值就不推理；busy 时全速        （gate.py）
   ↓
人体检测      YOLOv8n ONNX（cv2.dnn，2 线程，无需 torch）
              engine=auto 时没 ONNX 才退回 ultralytics            （onnx_detector.py）
   ↓
连续帧确认    detector.trigger_frames 帧连续命中 → ACTION_START     （rules.py 的 TriggerState）
   ↓
挑最好一帧    detector.capture_window 帧内按 frame_score() 取最大最居中（rules.py 的 frame_score）
   ↓
落盘 + 开录   data/captures/日期/HHMMSS.jpg + 同名 .mp4
              record.codec=avc1 / record.pre_roll_seconds=8        （recorder.py）
   ↓
人脸检测      YuNet ONNX，在 YOLO 框内找脸，返回 15 维行（含关键点）  （detector.py）
   ↓
人脸识别      SFace 128 维特征 + 余弦；identity.threshold=0.45
              identity.confirm_frames=2 帧确认                      （identity.py）
   ↓
分流          本人 → on_owner=skip（不抓拍不录像不播报）
              他人/认不出 → unknown_policy=alert（照常告警）
   ↓
播报 + 推送    SAPI5 离线合成（voice.require_face=true）
              notify.channel = wxpusher / serverchan / wecom_bot / none
              wecom_bot 支持图（base64）与录像（转码→upload_media→file）
              （notifier.py 发图；videopush.py 发视频，走后台队列）
   ↓
本地相册      gallery.py，127.0.0.1:8765，jpg 当封面、点开播 mp4
   ↓
归档清理      snapshot.keep_days=30 + storage.max_total_gb=20
```

### 模块职责

| 文件 | 职责 | 备注 |
|---|---|---|
| `roomwatch/watcher.py` | 主循环 + 命令行入口 | 采集/检测/抓拍/录像/语音/推送/清理 |
| `roomwatch/camera.py` | 采集源解析：USB / RTSP / 文件，后端选择、超时、退避 | 日志里密码必须脱敏 |
| `roomwatch/gate.py` | 静止门控：帧差不过阈值就不推理 | busy 时旁路，别把「站着不动的人」滤掉 |
| `roomwatch/onnx_detector.py` | 人体检测：YOLOv8 ONNX（cv2.dnn）+ NMS；torch 兜底 | 运行时**不许**顶层 import torch |
| `roomwatch/rules.py` | **纯逻辑层**：连续帧、挑帧、冷却、录像收尾、清理 | 不依赖摄像头和网络，单测覆盖率最高，新逻辑优先放这里 |
| `roomwatch/recorder.py` | 环形 JPEG 预录 + 后台线程写 H.264 | |
| `roomwatch/audio_recorder.py` | 录音 + ffmpeg 并音轨 | |
| `roomwatch/audio_device.py` | 系统音量读写（pycaw） | |
| `roomwatch/detector.py` | 人脸检测（YuNet，Haar 兜底） | 原 `faces.py` |
| `roomwatch/identity.py` | 人脸识别（SFace）+ 多帧投票 | |
| `roomwatch/speaker.py` / `loudness.py` | SAPI5 语音 / 响度增益 | |
| `roomwatch/notifier.py` | 推送渠道（文字 + 图片） | |
| `roomwatch/videopush.py` | 录像推送：ffmpeg 瘦身 → `upload_media` → 发文件 | 后台单线程队列，队列满即丢弃，不阻塞主循环 |
| `roomwatch/gallery.py` | 本地相册 HTTP | 只用标准库，自实现 Range |
| `roomwatch/guardian.py` | 守护 watcher + gallery（暂停/退避/单实例/通知） | |
| `roomwatch/health.py` | 健康快照、退出码、退出记录、停滞判定 | |
| `roomwatch/diagnostics.py` | faulthandler 与 excepthooks | |
| `roomwatch/logutil.py` | 日志轮转与固定字段事件 | |
| `roomwatch/diag.py` | 脱敏诊断包 | **不收 `data/`** |
| `roomwatch/benchcodec.py` | 编码基准（cv2 + ffmpeg 硬件编码器） | 只测量，不在主链路 |
| `roomwatch/benchdetect.py` | 推理基准（墙钟 + CPU 核数） | 只测量，不切换引擎 |
| `roomwatch/selfcheck.py` | 启动自检（模型/目录/推送/磁盘） | |
| `roomwatch/verify.py` | 「真的在工作」验证：自检+摄像头+推送+心跳 | 所有外部动作可注入 |
| `roomwatch/procutil.py` | 进程判活 / 等退出 / 结束 | 判活的唯一实现，updater 也用它 |
| `roomwatch/control.py` | 停止协议 + 期望状态 + gallery PID | 不许再用 taskkill 当主路径 |
| `roomwatch/settings.py` | 界面设置的后端：读生效值、写 `config.local.yaml`、复读校验 | 凭据只回掩码 |
| `roomwatch/autostart.py` | 开机自启（计划任务优先，Run 键回退） | 向导与界面设置共用 |
| `roomwatch/setup_push.py` | 推送配置引导 | |
| `roomwatch/gui.py` | 桌面界面（主窗口） | 独立进程，不 import cv2 / yaml |
| `roomwatch/gui_settings.py` | 设置窗口（四个页签） | 同样不 import tkinter/yaml，靠注入 |
| `roomwatch/cli.py` | 全局命令表与参数翻译 | 新增命令先改这里 |
| `roomwatch/updater.py` | 自动更新：Releases → 版本策略 → 下载校验 → 停占用 → 静默安装 | 下载域名白名单；SHA256 不过不许装；装前必须 `stop_all()` |
| `roomwatch/paths.py` | 项目根 / 运行时目录 / 解释器发现 | 路径基准的唯一来源 |
| `roomwatch/config.py` | 配置加载：主配置 + `config.local.yaml` 深合并 + 路径归一 | 别在别处直接 `yaml.safe_load(config.yaml)` |

---

## B. 环境与解释器（最容易踩的一条）

### B.1 主环境（跑监控）

项目要求 **Python ≥ 3.10**，开发与打包实测用 3.13。依赖以 `requirements.txt` 为准：

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

主要依赖：`opencv-python`（含 `cv2.dnn`）、`numpy`、`PyYAML`、`requests`、
`sounddevice`、`pyttsx3`、`psutil`。**不需要 torch / ultralytics** ——
人体检测走 ONNX + `cv2.dnn`，`requirements.txt` 里已移除。

把本项目跑起来后，`rw doctor` 会打印它实际选中的解释器；若与预期不符，
用环境变量 `ROOMWATCH_PYTHON` 显式指定。

**常见陷阱**：机器上往往装了多个 Python，其中一部分没有 `cv2`（监控跑不起来）
或没有 `tkinter`（界面打不出来）。判断依据只有 `rw doctor` 的输出，不要凭
`python --version` 猜。

验证：`rw doctor`（会打印解释器、依赖、模型、目录、端口、钩子）。

### B.2 界面环境（打 exe）

`scripts\build-exe.ps1` 会自动找系统里**带 tkinter** 的 Python（3.10+）建 `.venv-gui`，
只装 Pillow + PyInstaller。产物 `dist\room-watch.exe`（单文件，内嵌 tkinter）。
**不要试图用主环境打 GUI**：缺 tkinter 时 PyInstaller 会失败。

### B.3 `rw` 如何选解释器

`paths.find_python()` 顺序：`ROOMWATCH_PYTHON` →（分发包内）`runtime\python`
→ 当前解释器 → `python`。`rw.cmd` 在 Windows 上会优先试项目内的内嵌运行时，
所以双击和命令行都不需要配置 PATH。

### B.4 自动更新与版本纪律的关系

`rw update` 把版本纪律写进了代码：同 `主.次` 只前进修订号才自动装，
跨次版本必须 `--yes`。改 `updater.decide()` 之前先确认这是不是要放开规则 ——
它由 `tests/test_updater.py::DecideTest` 钉死，测试名就是规则本身。

更新链路是全项目唯一「下载可执行文件并运行」的地方，三条不许妥协：

1. 下载域名白名单（`github.com` / `objects.githubusercontent.com`）+ 强制 https；
2. SHA256 校验不过就删包并失败，`require_checksum` 默认 true；
3. 安装前必须先停 watcher（否则 runtime 里的 dll 被占用，安装器替换失败）。

GitHub 匿名 API 限流 60 次/小时，被限流时走 `/releases/latest` 的 302 拿 tag。
不要为此引入 token —— 客户端不该存任何凭据。

2026-10-19 实测：这次真实网络下 API 正是被限流的，**回退路径连资产地址拼装与
SHA256 校验都跑通了**（下载 142.1MB、摘要逐位一致），不是只在单测里成立。
但回退路径拿不到 `assets` 列表，`Release.installer_asset()` 是按命名约定合成的，
所以 G.4 的资产命名一旦改了，回退路径会静默找不到包。

**界面自己触发更新时，必须先退出界面进程**（0.1.5 起的 `--wait-exit <PID>`）：
界面是 `room-watch.exe`（PyInstaller），进程活着时安装器替换不掉这个文件，
表现为「提示更新成功、版本号却没变」。所以 GUI 的做法是 —— 把自身 PID 交给
`rw update --wait-exit <PID> --relaunch-gui`，自己销毁窗口，装完由子进程重开界面
（`updater.gui_launcher()` 优先用包内 `room-watch.exe`，内嵌解释器没有 tkinter）。

界面里**不重写版本比对**：一律走 `rw update --json`，让 `updater.decide()` 保持
唯一真相。检查结果缓存在 `var/state/update.json`，按 `update.check_interval_hours`
复用（0 = 关闭后台自检），启动自检只改按钮文字，不弹窗、不自动安装。

---

## C. 全局命令

统一入口 `rw`（Windows 用根目录 `rw.cmd`，bash 用根目录 `rw`，等价 `python -m roomwatch`）。
命令表在 `cli.py` 的 `COMMANDS`，**新增功能先在那里登记**，否则 `rw --help` 里看不到。

| 目的 | 命令 |
|---|---|
| 环境自检 | `rw doctor` |
| 诊断包（脱敏） | `rw diag` |
| 拍预览图 | `rw probe` |
| 试跑（不推送） | `rw dry-run` |
| 后台启动（guardian 托管） | `rw start`；前台调试用 `rw start --foreground` |
| 看是否在线 | `rw status` |
| 注册人脸 | `rw enroll` / `rw enroll --from IMG` |
| 验证识别 | `rw whoami IMG` |
| 试听语音 | `rw say` / `rw say-raw` / `rw ab` |
| 音量 | `rw volume-check` / `rw volume-set 70` |
| 磁盘占用 | `rw storage`（`--json` 供界面/脚本） |
| 编码基准 | `rw bench-encode`（只测量，不改默认编码器） |
| 推理基准 | `rw bench-detect`（只测量，不切换引擎） |
| 验证「真的在工作」 | `rw verify [--json] [--no-camera]` |
| 相册 | `rw gallery [--port 9000]` |
| 守护（监控 + 相册） | `rw guard [--once\|--status\|--dry-run]` |
| 推送配置 | `rw push` |
| 验证录像推送 | `rw push-video PATH`（需 webhook 已配好） |
| 测试 | `rw test [--fast]` |
| 版本 | `rw version`（带 git 短哈希） |
| 暂停并停止（优雅） | `rw stop`（同时停相册；之后需 `rw start` 恢复） |
| 读/改设置 | `rw config`（`--json` / `--set 键=值` / `--test`） |
| 开机自启 | `rw autostart`（`--json` / `--enable` / `--disable`） |
| 本人模板 | `rw identity`（`--json` / `--clear`） |
| 桌面界面 | `rw gui` |

每个子命令都支持追加原模块参数（`--config config.local.yaml` 等）。
`cli.py` **只做翻译**，参数校验仍由各模块 argparse 负责；取值缺失时抛 `UsageError`（退出码 2）。

启动脚本：`scripts/start-watcher.bat`（只起监控）、`scripts/start-guardian.bat`（守护+监控）、
`scripts/install-autostart.ps1`（计划任务，需本人执行）、`scripts/install-shortcut.ps1`（桌面快捷方式）。

---

## D. 目录与路径约定

### D.1 三条放置规则

1. 只有 `roomwatch/` 放源码；仓库根只留 `rw` / `rw.cmd` / 配置 / 元数据 / 资产。
2. `data/` 放隐私数据（抓拍、录像、人脸模板），`var/` 放可重建产物（日志、状态、缓存、预览图）。
   判断标准：删掉会不会丢用户数据。
3. 配置里的相对路径一律**相对项目根**，经 `paths.resolve()` 解析；
   不要在业务代码里用 `dirname(__file__)` 拼数据路径。

### D.2 为什么第 3 条是硬规则

包化前 `detector.py` / `identity.py` 用 `dirname(__file__)` 找 `models/`，
模块一挪就找不到模型，而**检测器只是静默不工作，不报错**。
`guardian.py` 也曾用 `HERE + "watcher.py"` 拉起子进程，包化后直接失效。
现在统一走 `paths.resolve()`，并由 `tests/test_paths.py` 钉住。

### D.3 运行时目录一览

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `data/captures/<日期>/` | 抓拍 jpg、录像 mp4、音频 wav | 否（隐私） |
| `data/identity/owner.npz` | 本人人脸特征 | 否（生物特征） |
| `var/logs/` | watcher / gallery / probe / gui-selftest / watcher-fault | 否 |
| `var/state/` | status.json / health.json / last_exit.json / guardian_state.json / guardian_history.jsonl / guardian.lock / **guardian.owner.lock** / **desired_state.json** / **gallery.pid** / **stop.request** / **volume_backup.json** | 否 |
| `var/db/events.db` | 事件库 | 否 |
| `var/media/` | preview.jpg 等临时产物 | 否 |
| `var/cache/voice/` | 语音缓存（可重建） | 否 |
| `var/diag/` | `rw diag` 生成的诊断包（可删） | 否 |
| `models/` + `yolov8n.pt` | 权重 | **是**（下载极慢，属稀缺资产） |

### D.4 分发布局（0.1.3 起）

`scripts/build-portable.ps1` 产出的免安装目录，**根就是 `PROJECT_ROOT`**：

```
room-watch-<版本>\
├─ room-watch.exe          界面壳（PyInstaller 打的，只有 tkinter）
├─ rw.cmd / 配置.bat / 启动.bat / 自检.bat
├─ config.yaml             占位串
├─ roomwatch\              源码
├─ models\                 权重
├─ runtime\python\         嵌入式 Python + Lib\site-packages
├─ tools\ffmpeg.exe
└─ data\ var\
```

三条硬约束：

1. **不要把源码放进 `app\` 子目录。** `python -m roomwatch` 依赖「当前目录在
   `sys.path` 里」；多一层就得维护 `PYTHONPATH`，而计划任务 / Run 注册表这类
   拿不到工作目录的启动方式会静默失败（进程秒退、日志为空）。
2. **`config.local.yaml` 绝不打进分发包。** 里面是本机的 webhook 与相册密码。
   构建脚本会显式跳过并打印提示。
3. **客户机上不能出现只有本机才有的路径。** `paths.find_python()` 的顺序是
   `ROOMWATCH_PYTHON → 包内 runtime/python → 本机已知环境 → PATH`，
   所以打包后一律走内嵌解释器；不要在业务代码里写死 `C:\Users\...`。

内嵌发行版默认把 `Lib\site-packages` 排除在 `sys.path` 之外，装依赖前必须
改 `python313._pth`（加一行 `Lib\site-packages` 并取消 `#import site` 的注释），
这一步由构建脚本完成，手工重建 runtime 时别忘。

### D.5 Inno Setup 安装包（0.1.3 起）

`scripts/build-installer.ps1` + `scripts/room-watch.iss` 把 D.4 的目录打成
单个 `setup.exe`。五个必须记住的点：

1. **Inno 官方包不含简体中文。** `Languages\` 下只有 29 种官方语言，
   写 `ChineseSimplified.isl` 会直接编译失败。语言文件放在
   `scripts\inno\ChineseSimplified.isl`（取自 issrc 的 `Unofficial` 目录，
   tag `is-6_7_3`），且**必须带 UTF-8 BOM**，否则中文界面是乱码。
2. **`[UninstallRun]` 必须给 `RunOnceId`。** 没有它，升级时新旧两个卸载器
   各执行一次 `reg delete`，编译器也会报 warning。
3. **卸载不要删 `config.local.yaml` / `data\` / `var\db`。** Inno 升级流程是
   「先跑旧卸载器，再装新版」；卸载器一删配置，客户每次升级都要重配摄像头和
   webhook。只删 `var\logs`。
4. **`runtime\` 要用 `filesandordirs` 强删。** `comtypes` 会在
   `site-packages\comtypes\gen\` 运行时生成 `SpeechLib.py` 等 5 个文件，
   它们不在安装清单里，不显式删就留下残骸、目录也删不掉。
5. **开机自启不要放 `[Tasks]` 再传给向导。** 勾选任务和向导第 4 问会互相矛盾
   （取消勾选仍自启）。统一交给 `rw setup` 的第 4 问，安装器只负责拉起向导。

**瘦身边界**：`win32comext` 可整棵删（pyttsx3 的 SAPI5 只走 `win32com`，
实测删后仍能枚举 2 个语音）；`win32\lib\win32con.py` **不能删**，删了语音直接不可用。

---

## E. 测试

24 个文件、544 项，全部可离线运行（`tests/` 下，均带 `unittest.main()`）：

| 文件 | 用例数 | 命令 | 需要什么 |
|---|---:|---|---|
| `test_logic.py` | 56 | `python tests/test_logic.py` | 纯标准库 |
| `test_loudness.py` | 32 | `python tests/test_loudness.py` | numpy |
| `test_recorder.py` | 19 | `python tests/test_recorder.py` | cv2，真写视频（约 3 秒） |
| `test_gallery.py` | 18 | `python tests/test_gallery.py` | cv2，真起 HTTP（约 17 秒，占 8765） |
| `test_guardian.py` | 31 | `python tests/test_guardian.py` | yaml + psutil，不真重启 |
| `test_identity.py` | 31 | `python tests/test_identity.py` | cv2 + 合成图（2 项按设计跳过） |
| `test_audio_storage.py` | 27 | `python tests/test_audio_storage.py` | 临时文件 |
| `test_videopush.py` | 22 | `python tests/test_videopush.py` | 纯逻辑，ffmpeg 与 HTTP 都 mock |
| `test_config.py` | 14 | `python tests/test_config.py` | 临时配置文件 |
| `test_setup_push.py` | 10 | `python tests/test_setup_push.py` | 纯逻辑，不联网 |
| `test_notifier.py` | 10 | `python tests/test_notifier.py` | 纯逻辑，HTTP 全 mock |
| `test_integration.py` | 6 | `python tests/test_integration.py` | 假摄像头 + 假检测器（约 55 秒） |
| `test_cli.py` | 18 | `python tests/test_cli.py` | 纯逻辑，不启进程 |
| `test_paths.py` | 12 | `python tests/test_paths.py` | 纯逻辑 |
| `test_gui_state.py` | 15 | `python tests/test_gui_state.py` | 临时状态库，不建窗口 |
| `test_camera.py` | 15 | `python tests/test_camera.py` | 纯逻辑，不碰摄像头 |
| `test_gate.py` | 8 | `python tests/test_gate.py` | 合成图，钉住事件期间不跳帧 |
| `test_onnx_detector.py` | 13 | `python tests/test_onnx_detector.py` | 合成张量，不加载模型 |
| `test_gallery_auth.py` | 18 | `python tests/test_gallery_auth.py` | 真起 HTTP，约 2.5 秒 |
| `test_procutil.py` | 8 | `python tests/test_procutil.py` | 纯逻辑，不真杀进程 |
| `test_control.py` | 10 | `python tests/test_control.py` | 临时目录，注入假判活 |
| `test_settings.py` | 16 | `python tests/test_settings.py` | 临时项目 + 临时配置 |
| `test_autostart.py` | 12 | `python tests/test_autostart.py` | 不碰真注册表（读写注入） |
| `test_volume_mode.py` | 15 | `python tests/test_volume_mode.py` | patch 掉 pycaw 调用 |

全量：`rw test`（约 90 秒）；`rw test --fast` 跳过集成测试。

> 2026-10-19 v0.1.2 实测：19 个文件全部 `OK`，共 375 项。
> 2026-10-19 v0.1.9 实测：24 个文件全部 `OK`，共 538 项。
> 2026-10-19 v0.1.10 实测：24 个文件全部 `OK`，共 544 项。

---

## F. 踩过的坑 / 硬约束

每条都是真实事故，按「现象 → 原因 → 正解」记录。

### F.1 摄像头同时只能被一个进程占用；改完代码必须重启

- **现象**：第二份监控打不开摄像头；改完 `config.yaml` 后运行中的进程行为不变。
- **原因**：设备独占；摄像头/模型等配置只在 `Watcher.__init__` 里读一次。
  **0.1.21 起**「更新 / 自启 / 声音」三页保存后热更新（reload 标记），
  不再需要重启；其余设置仍按原样重启生效，界面会逐项提示。
- **正解**：改完代码或配置，必须重启监控与相册。不要靠「再开一份」验证。
- **另**：**禁止同时跑两组实例**（2026-10-18 实测出现过两个 guardian + 两个 watcher
  同时存在的局面，互相抢摄像头）。发现重复实例先 `rw status` 看 PID，再确认后终止。
- **RTSP 不受此限**：网络摄像头允许多个客户端同时拉流，但仍建议只跑一份——
  两份都会推送、都会录像，重复告警比抢设备更烦。

### F.2 静止门控不能把「站着不动的人」滤掉

- **现象**（若写错）：人进屋后站着不动，帧差归零 → 不推理 → 20 秒后录像被判「人走了」收尾。
- **正解**：三道保险缺一不可——`busy`（录像中/刚检测到人）时无条件推理；
  空闲时每 `gate.force_every_seconds` 至少推理一次；门控只决定跑不跑模型，不改检测语义。
- **改动 gate.py 后必须跑** `tests/test_gate.py`，里面直接钉了这四条行为。

### F.3 运行时不许把 torch 拉回来

- **背景**：torch 539MB 且默认吃满所有核，是「依赖乱、CPU 打满」的根因。
- **约束**：顶层 import 里不能出现 `torch` / `ultralytics`；`onnx_detector.py` 里
  只在 `TorchDetector.__init__` 和 `export()` 内部惰性 import。
- **验证**：`grep -rn "ultralytics" roomwatch/` 只应命中注释与这两个函数体。

### F.4 判断是否活着 = 心跳新鲜 **且** PID 还在

- **现象**：进程被强杀后，`status.json` 里的 `stale` 仍是 `false`，把死的报成在线。
- **原因**：进程被杀前来不及再写状态。
- **正解**：在读取那一刻重算 `now - last_frame_at > heartbeat.stale_after_seconds`。
- **2026-10-19 补充（真机事故）**：只算心跳还不够。0.1.7 的界面在 `taskkill /F`
  之后仍把进程报成在线，因为 `last_frame_at` 还很新（≤120 秒），
  于是「启动监控」按钮一直禁用 —— 用户看到的是「停止只停到一半」。
  现在 `gui.read_state()` 必须同时满足 `procutil.pid_alive(pid)` 与心跳新鲜；
  判活的唯一实现是 `procutil.pid_alive()`（`updater` 已改为委托它）。

### F.5 本机 `Start-Process`（PowerShell）无法启动进程

- **现象**：报「已添加项。字典中的关键字:'Path'所添加的关键字:'PATH'」。
- **正解**：启动后台进程一律用 bash 后台方式，或 `subprocess.Popen` +
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`（guardian 与 GUI 都这么做）。

### F.6 录像必须用 avc1(H.264)

- **现象**：日志刷 `Failed to load OpenH264 library`。
- **原因**：本机 OpenCV 5.0 只有 avc1 与 mp4v 能写；浏览器基本只认 H.264。报错来自
  FFmpeg 后端，实际由 Media Foundation 接手成功编码。
- **正解**：`record.codec` 保持 `avc1`；那条 stderr 是噪音。mp4v 只在兜底时用，网页放不了。

### F.7 H.264 编码 11~18ms/帧，必须后台线程写盘

- **正解**：`record.queue_size`（默认 120）满时宁可丢帧，也不许拖慢检测主循环。

### F.8 录像有 8 秒预录，合并音轨要 `adelay`

- **现象**：并好音轨后声音整段晚 8 秒。
- **原因**：预录画面发生在开始录音之前，音轨天然比视频短。
- **正解**：`-af adelay=<预录毫秒>:all=1`，视频流 copy 不重编码
  （`audio_recorder.py` 拼参数处；`test_audio_storage.py` 有断言）。

### F.9 `alignCrop()` 必须传原图与原图坐标系的关键点

- **现象**：人脸识别认错人，但不报任何错。
- **原因**：YuNet 在 YOLO 人形框的裁剪图上跑，返回的是裁剪图坐标；直接交给
  `alignCrop()` 会把脸摆歪、特征全废。
- **正解**：用 `detector.py` 的 `_shift_row()` 补回偏移。任何新增的人脸相关调用都走同一条路。

### F.10 编辑 `config.yaml` 用最小增量并复读

- **现象**：`behavior.idle_threshold_seconds` 曾被吞进上一行注释里，配置项静默失效。
- **正解**：只改目标行，改完立刻 grep 该键确认还在、缩进没变。不要顺手重排整个文件。

### F.11 `events_today` 是内存计数，重启归零

- **正解**：真实事件数查 `var/db/events.db`（`storage.database`）。

### F.12 桌面界面没有 tkinter 可用时不要让用户自己猜

- **现象**：主环境 `import tkinter` 报 `ModuleNotFoundError`。
- **正解**：`gui._relaunch_with_tk()` 自动换成有 tkinter 的解释器重启自己；
  打包后的 exe 内嵌 tkinter，不受影响。

### F.13 打包参数里的相对路径按 spec 目录解析

- **现象**：`--add-data VERSION;.` 与 `--icon assets\x.ico` 在配合 `--specpath build` 时
  报「找不到文件」。
- **正解**：这两个参数用**绝对路径**（`scripts/build-exe.ps1` 已这么写）。

### F.14 推送层三个静默失效点（2026-10-18 真机连中三个）

推送是最容易「看起来配好了其实没发」的模块，这三个都表现为**没有任何报错**：

1. **`send(text, image_bytes)` 的第二参数是图片字节**。`maybe_heartbeat` 曾写成
   `send("监控在线", text)`，正文被当图片做 base64 → TypeError → 整条失败。
   企业微信下日志能看见 `a bytes-like object is required`；wxpusher 下只取第一个
   参数，于是永久只发「监控在线」四个字，正文全丢。
2. **`sendkey.startswith("SCT")` 把真 key 当占位符**。Server酱 的真实 SendKey 就是
   SCT 开头。凭据判定一律用 `"replace_me" in value.lower()`，不要用前缀。
3. **webhook 双嵌套**（详见 G.2）。文字接口宽松、`upload_media` 严格，
   表现为「文字收得到、录像发不出」。

排查口诀：文字通了不代表通道通了；先用 `rw push-video` 打一次真实录像。

### F.15 窗口程序没有控制台，排障靠追踪日志

- **现象**：exe 双击没反应，看不出卡在哪。
- **正解**：设 `ROOMWATCH_GUI_TRACE=1` 后启动，阶段写入 `var/logs/gui-trace.log`；
  `dist\room-watch.exe --selftest` 会建窗→刷新→销毁并写 `var/logs/gui-selftest.log`。
  曾靠它发现 `gui.main()` 没读 `sys.argv`、导致 `--selftest` 被忽略的 bug。

### F.16 客户端断开 ≠ 服务端故障（Windows 是第三个异常类）

- **现象**：`rw gallery` 挂着两小时，日志里 23 段
  `Exception occurred during processing of request`，每段 30 行堆栈，
  末行 `ConnectionAbortedError: [WinError 10053] 你的主机中的软件中止了一个已建立的连接`。
- **成因**：浏览器关页面、拖视频进度条会掐掉上一条连接。拔连接有三种落法，
  **类不同**：`BrokenPipeError`（EPIPE）、`ConnectionResetError`（10054，RST）、
  `ConnectionAbortedError`（10053，正常 FIN 后服务端继续写）。
  原代码只捕前两个，10053 直接漏到 `socketserver.handle_error` 打成故障。
- **正解**：统一用 `gallery.CLIENT_GONE` 四元组；两层都拦 ——
  `serve_media()` 的发送循环捕异常，`GalleryServer.handle_error()` 兜住
  写响应头/首页/401 阶段的同类断开，其余异常照旧打印。
- **鉴别力**：`tests/test_gallery.py::TestClientDisconnect` 用真 socket 跑两种断法
  （`SO_LINGER` 造 RST、普通 `close()` 造 10053），断言 stderr 无 `Traceback`
  且服务之后仍能回 200。把 `CLIENT_GONE` 清空即复现满屏堆栈。
- **反例提醒**：别用 `except OSError: pass` 一把抓 —— 磁盘读失败也会被吞掉。
  文件打开错误单独回 500，只有发送阶段的断开才静默。

### F.17 停止监控必须走 stop.request，不许回到 taskkill

- **背景**：0.1.7 的界面停止按钮就是 `taskkill /F`。三个后果全在真机上出现过：
  正在写的 mp4 头没写完（相册里点开是坏的）、音轨没合并、抬高过的主音量不还原。
- **正解**：`control.stop_watcher()` 两段式 —— 先写 `var/state/stop.request`
  （JSON，含目标 PID），watcher 主循环每秒查一次，命中就 `break`，
  走既有的 `finally` 收尾；等 `control.DEFAULT_GRACE`（20 秒）还没退，
  再由**用户确认**后强杀（`gui.force_kill_command`）。
- 请求文件必须带 PID：PID 会复用，残留请求不许秒杀新进程；
  watcher 启动时还会 `clear_stop_request()` 清一次残留。
- 界面在停止过程中显示「正在停止…（录像收尾中）」，两个按钮同时锁住。
- 实测（2026-10-19）：优雅停止 1 秒完成，`stop.request` 自动清理。

### F.18 播报音量与录音无关，别拿系统主音量凑响度

- **现象**：日志 `系统音量：主音量 2.0% → 70.0%`。用户每次开始监控，
  电脑的系统音量就被改；强杀后还原不回来，音量永久停在 70%。
- **原因**：`voice.ensure_system_volume: 70` 会调 `SetMasterVolumeLevelScalar`。
  而**录音走麦克风输入**，与系统播放音量毫无关系——抬主音量只影响 TTS 播报。
- **正解**：`voice.volume_mode` 三档，默认 `off`：
  `off` 完全不碰 / `session` 只改本进程音轨（音量合成器里那条）/
  `master` 旧行为（显式选择，界面有警示）。判定统一走 `audio_device.resolve_mode()`。
  `master` 档改前会写 `var/state/volume_backup.json`，
  下次启动发现记录里的进程已不在就自动补还原（`restore_backup()`）。

### F.19 界面设置一律写 config.local.yaml

- **原因**：安装包 `[Files]` 是 `Source: {#SourceDir}\*` + `ignoreversion`，
  **升级会整体覆盖 `config.yaml`**。界面把用户设置写进主配置，下次更新就静默丢失，
  症状是「明明存过又变回去了」。
- **正解**：`settings.apply()` 只写 `config.local.yaml`（深合并、升级保留），
  与 `rw push` / `rw setup` 一致；写完必须 `load_config()` 复读校验，
  不一致就报错（不许静默成功）。
- 界面进程（打包后）只有 tkinter + Pillow：读配置走 `rw config --json` 子进程，
  **不要在 gui 里 import yaml**。

### F.20 密钥只能回掩码

- `settings.snapshot()` 对凭据只返回 `{set, tail}`（末 4 位），全文不出进程；
  界面显示「已设置 …5179（留空＝不改）」，输入框留空表示不修改。
- 判定「占位串」只看 `replace_me`（与 `setup_push` 一致）。
  用前缀判定会把 Server酱真实 key（SCT 开头）当成占位符——踩过一次。

### F.21 YAML 1.1 把裸 off/on 当布尔

- **现象**：`config.yaml` 写 `volume_mode: off`，读出来是 `False`，
  界面上那一项变成空、后端还以为模式未知。
- **正解**：配置文件里这类值加引号（`volume_mode: "off"`）；
  `resolve_mode()` 与 `settings.snapshot()` 也对布尔做了兜底解释。
  `yaml.safe_dump` 会自动给这种字符串加引号，所以界面写的 local 文件没问题。


### F.22 更新前必须停掉所有占用安装目录的进程

- **现象（2026-10-19 真机）**：从 0.1.7 升级 0.1.9，安装器返回 **rc=5** 并整段
  回滚，`VERSION` 原地不动；随后脚本自动重开界面，弹出原生错误框
  `Failed to load Python DLL ... _MEIxxxx\python313.dll`。
- **原因**：`rw update` 只停了 watcher。相册（`python -m roomwatch gallery`）
  同样跑在包内 `runtime\python` 上，占着 `python313.dll`；Inno 装前的
  RestartManager 关不掉这种没有消息循环的控制台进程，30 秒后按 Abort。
  失败分支立刻 `start` 界面，又和回滚收尾抢时间，于是 PyInstaller 解包被打断。
- **正解**：装之前调 `updater.stop_all()` —— 按「可执行文件在安装目录下 +
  命令行含 roomwatch」挑出所有进程，watcher 走优雅停止（`control.stop_watcher`），
  相册/守护直接 `terminate → kill`；失败分支先 `ping -n 3` 等两秒再开界面。
- **教训**：任何「会写 runtime\」的流程（安装、替换运行时）都要先把占用者数清楚，
  不能只盯着 watcher。`select_targets()` 是纯函数，测试里直接喂进程列表。

### F.23 健康快照与退出记录是两套文件

- `var/state/health.json` 是「现在」：每秒更新，回答进程在不在、循环序号、
  连续错误数、摄像头重开次数、RSS/CPU、自检结果。
- `var/state/last_exit.json` 是「上一次」：只在进程退出时写，回答为什么退出。
- **退出码**：`0` 正常 / `2` 配置 / `3` 摄像头 / `4` 模型 / `5` 未处理异常 /
  `6` 停滞。guardian 重启时把退出码与原因写进 `guardian_history.jsonl`。
- 原生崩溃（cv2/驱动层）由 `faulthandler` 写 `var/logs/watcher-fault.log`；
  WER LocalDumps 默认不开，需要注册表写权限，只在排障时手动启用。
- 反馈问题先跑 `rw diag`：诊断包会脱敏配置、只收 `var/` 与配置，**不收 `data/`**。

### F.24 用户暂停后 guardian 不许拉起

- `var/state/desired_state.json` 是用户意图：`running` / `paused`；缺失或损坏按 `running`。
- `rw stop` / 界面「停止监控」：先写 `paused`，再停 gallery 与 watcher；guardian 看到
  paused 直接返回 `ACT_PAUSED`，不启动/重启任何服务，也不发「已恢复」通知。
- `rw start` / 界面「启动监控」：写 `running`，启动常驻 guardian，并跑一次
  `guard --once` 立即检查，避免等一个 `check_seconds` 周期。
- 常驻 guardian 用 `guardian.owner.lock` 保证单实例；`--once` 只用原来的短锁。
- gallery 用 `gallery.pid` + 命令行（必须同时含 `roomwatch` 与 `gallery`）判活，
  PID 复用不会误杀；挂了由 guardian 拉起并写历史。

---

## G. Git、隐私与钩子

### G.1 什么入库

- **入库**：`roomwatch/**`、`tests/**`、`scripts/**`、`docs/**`、`rw`、`rw.cmd`、
  `config.yaml`、`README.md`、`AGENTS.md`、`CHANGELOG.md`、`VERSION`、`pyproject.toml`、
  `requirements.txt`、`models/*.onnx`、`yolov8n.pt`、`assets/*.ico`、`.githooks/`。
- **不入库**：`data/`、`var/`、`config.local.yaml`、`.workbuddy/`、`dist/`、`build/`、
  `.venv-gui/`、`__pycache__/`。

### G.2 凭据

`config.yaml` 里四个凭据键入库时必须是占位串：
`notify.wxpusher.app_token` / `notify.wxpusher.uid` / `notify.serverchan.sendkey` /
`notify.wecom_bot.webhook`。

真实凭据只放 `config.local.yaml`（已忽略），由 `rw push` 自动生成；
`config.load_config()` 启动时深合并，不用改启动命令。

**webhook 必须是标准地址**：`.../webhook/send?key=<key>`。
2026-10-18 踩过——旧版 `rw push` 把用户粘的完整地址替换到 `key=REPLACE_ME` 的位置，
得到 `?key=https://...?key=xxx` 的双嵌套。文字消息接口校验宽松照样能发，
`upload_media` 严格校验直接 `40058 invalid param 'key'`，症状是「文字能收到、视频永远发不出」。
现在 `setup_push.normalize_wecom_webhook()` 统一剥 key，`tests/test_setup_push.py` 钉住。

**泄漏风险靠提交前钩子拦，不靠忽略文件。** 钩子：`.githooks/check_secrets.py`（判定）
+ `.githooks/pre-commit`（薄壳）。启用：`scripts\setup-hooks.bat`（幂等，会回读回显
`hooksPath : .githooks`）。`core.hooksPath` 是本地配置，**不随仓库分发**，新克隆必须重跑。

自测：`python .githooks/check_secrets.py --self-test`（23 项）。

### G.3 换行与 BOM

- `.gitattributes`：`*.bat/*.ps1/*.cmd` = CRLF；`*.py/*.md/*.yaml` = LF；`rw` = LF；
  图片/音视频/npz/pt/onnx/db = binary。
- **三个 `.bat` 必须带 UTF-8 BOM，且前两行是 `@echo off 2>nul` 紧接 `@echo off`**：
  无 BOM 时 cmd 按 936 代码页解码，括号块里的中文会被劈开当命令执行；
  有 BOM 时 cmd 也不是每次都认，BOM 会粘到第 1 行，导致 echo 没关掉。
  两者叠加的解法就是这两行 + BOM。
- `.bat` 的 `rem` 注释保持 ASCII；中文 `echo` 行可以，但不要放进 `( )` 块。
- `.ps1`（`build-exe.ps1` / `install-shortcut.ps1` / `install-autostart.ps1`）带 BOM，
  否则 PowerShell 5.1 按 ANSI 解码，中文乱码。

### G.4 开源仓库与发版

- 仓库：<https://github.com/Alec-WangLi/room-watch>（public，主分支 `main`）。
- 许可证 **AGPL-3.0**，原因写在 `README.md`：`models/yolov8n.onnx` 来自 Ultralytics，
  本身即 AGPL-3.0，主许可证与之一致才不会自相矛盾。`NOTICE` 逐项列第三方来源。
- **发版两个通道，不要发错**（脚本幂等，重复跑只补缺失资产）：

  ```
  set GH_TOKEN=ghp_xxx        :: PowerShell: $env:GH_TOKEN="ghp_xxx"

  python scripts/release.py --channel stable
  python scripts/release.py --channel beta
  ```

  - `stable`：取 `main` 的提交，tag `v<版本>`，普通 Release。
    内置自动更新只认这个通道。
  - `beta`：取 `beta` 的提交，tag `v<版本>-beta`，**必须勾 prerelease**。
    GitHub 的 `/releases/latest` 不返回预发布，所以稳定用户不会被推测试版；
    测试者手动下载安装。
  - 脚本会校验已存在的 tag/release 是否与通道一致，发现指向别的提交或
    prerelease 标志不符会拒绝复用。先 `--dry-run` 可以只看将要做的事。
  - **Release 简介以 `CHANGELOG.md` 为唯一来源**：自动取对应版本的小节，
    不再只写安装包与 sha256。beta 从旧版本跳到新版本时用 `--notes-include`
    把未发布的中间版本一并写进简介；补写历史 Release 用 `--notes-only`
    （只更新简介，不动 tag 与资产）。
  - 资产命名两个通道一致：`room-watch-<版本>-setup.exe` + `.sha256`。
    仓库名可用 `RW_REPO` 覆盖（fork 后自己发版）。

- **所有者口径（2026-10-19）**：只要本人说「推送到远程」，就默认同时打 tag
  并发 GitHub Release；但仍必须先判断版本性质——`main`/稳定版走 `stable`，
  `beta`/测试版走 `beta`，不确定就先问，不许猜着发。
- 打 tag = 对外发布；除上述默认口径外，仍按第 12 条版本纪律需所有者批准。

### G.5 推送权限与历史重写（2026-10-19 真机踩过）

- **`workflow` scope 是硬门槛**：缺它的 token 推送 `.github/workflows/` 下任何文件，
  GitHub 会让**整条 push 被拒**（不是该文件单独失败），报
  `refusing to allow an OAuth App to create or update workflow ... without workflow scope`。
  所以 CI 定义当前放 `docs/ci-tests.yml`，启用方式见 `docs/publishing.md` 0.1 节（网页粘贴即可）。
- 换 token 不解决问题：本机 GCM 存的是 `gho_`（classic OAuth，有 `repo` 无 `workflow`）；
  GitHub CLI 设备流拿到的是 `ghu_`（GitHub App 用户 token），对**未安装该 App 的仓库直接 403**。
  真要用命令行改 CI，只能要一个带 `workflow` 的 classic PAT。
- **`git filter-repo` 会删掉 remote**：每次 `--replace-text` / `--invert-paths` 之后必须
  重新 `git remote add origin ...`，否则 `git push` 报「没有配置远程」。
- filter-repo 的替换规则文件**必须用 Python 写**：bash heredoc 会把 `\U` 吃掉；
  且 Windows 上 Python 的 `/tmp` 与 git-bash 的 `/tmp` 不是同一处（前者解析成 `C:\tmp`）。
- 历史已重写多轮（清真实 webhook key、清 `C:\Users\<本机名>`）。**任何旧克隆都不得再推**，
  否则会把清理掉的内容带回来；要重建就删库重建，别强推。

### G.6 分支策略（2026-10-19 所有者定）

- **`main` 是稳定版**：只接受已通过全量测试的合并，不直接在上面开发。
- **`beta` 是测试版开发分支**：新功能、实验、重构都从 beta 切特性分支，
  完成后先合回 beta 验证；稳定后再把 beta 合回 main。
- 特性分支命名建议 `feat/<主题>`；合并前必须跑 `rw test`（含集成测试）。
- 当前仓库的 `beta` 分支由 main 创建；首次合并后保持存在，不要删除。

---

## H. 给 AI 的行为约定

1. **动手前先读两份**：本文件 + `.workbuddy/memory/` 下最近的工作日志。
2. **新克隆第一件事跑 `scripts\setup-hooks.bat`**，确认回显 `hooksPath : .githooks`。
3. **未经批准不得杀死正在运行的 watcher / gallery**。确需重启，先看
   `rw status` 的 `recording` 是否为 false（硬杀会让当前 mp4 不可播放）。
4. **不要同时启动两份实例**（摄像头独占）。
5. **改 `config.yaml` 用最小增量并复读**（F.8）。
6. **代码风格跟随现有代码**：中文注释、类型注解、纯函数优先。
   新逻辑优先放 `rules.py`，不要塞进 `watcher.py`。
7. **任何新功能都要配可离线运行的单测**，并保证 `rw test` 全绿。
8. **不要编造命令**。本文件没写的，先 `rw --help` / `--help` 或读源码确认。
9. **提交前不要绕过钩子**，不要用 `--no-verify`；钩子阻断时按它打印的方法改。
10. **不要 `git add -f` 隐私文件**，不要删除 `data/captures/`（用户的数据）。
11. **引用用语义锚点，不写行号**；改完文档复读一遍，确认路径与命令与代码一致。
12. **版本号纪律（硬性）**：`VERSION` 的前两位是**项目所有者批准的基线**，记在
    `VERSION-APPROVED`。**没有本人明确同意，只许动第三位（修订号）** —— 一直停在测试版序列。
    要升 `0.2.0` 这类正式版，先取得同意，再把 `VERSION-APPROVED` 改成新基线，然后才动 `VERSION`。
    越界会被 `tests/test_paths.py::VersionTest::test_minor_bump_requires_approval` 判红。
    改版本号须同步在 `CHANGELOG.md` 顶部加一节，并跑 `rw version` 与 `rw test` 验证。
13. **不要替用户宣布「正式版」**。版本号里的 `0.x` 就是测试版，说「正式版」前先确认基线。
14. **不要凭空往 `.github/workflows/` 加文件**。当前 token 缺 `workflow` scope，
    加了会让整条 push 失败。CI 定义改 `docs/ci-tests.yml`，启用交给网页操作（G.5）。
15. **不要用 `git credential fill` 之外的方式猜凭据，也不要把取到的 token 打印出来**。
    2026-10-19 就因为脚本直接 echo 了凭据变量的值，把 token 打进了会话日志。
    读凭据只落文件、不落 stdout，用完立即删除。

---

## I. 已知待办（含为何暂缓）

1. ~~**gallery 的长期守护方案未定**~~（**0.1.12 已修**）—— guardian 现在同时守护
   watcher 与 gallery：gallery 写 `var/state/gallery.pid`，guardian 用 PID + 命令行
   校验判活，挂了拉起并写历史。保留这条记录，避免有人再退回「只守 watcher」。
2. ~~**录像在本人坐在电脑前时仍会录**~~（**0.1.17 已提供选项**）——
   `record.owner_at_computer` 三档 keep / mute / skip，默认 `mute`（照录不推，
   与旧版一致）；想连录像一起停下来的人显式设 `skip`。默认值不为省磁盘牺牲取证：
   你坐在电脑前时有人进来，仍然要录到。保留这条记录，避免有人把默认值悄悄改成 skip。
3. ~~桌面的「停止监控」不优雅~~（**0.1.9 已修**）—— 改成 `stop.request` + 两段式停止，
   见 F.17。保留这条记录，避免有人再「优化」回 `taskkill /F`。
4. **~~`install-autostart.ps1` 硬编码绝对路径~~（已在 v0.1.0 修掉）** ——
   现在用 `$PSScriptRoot` 推导项目根，参数改为 `-m roomwatch guard --once`，并补了 BOM。
   保留这条记录，避免有人再「优化」回硬编码。
5. **CI 未启用** —— 定义在 `docs/ci-tests.yml`，要所有者网页粘贴到
   `.github/workflows/tests.yml` 才生效。**暂缓原因**：token 缺 `workflow` scope（G.5），
   命令行推不上去。
6. **旧企业微信 webhook 必须作废重建** —— 那个 key 曾进过 git 历史（已重写清除），
   按泄露处理：企业微信后台删掉该群机器人重建，新 key 只进 `config.local.yaml`。
   **待所有者本人操作**，代码侧已无需改动。
7. **Watcher 每 30~63 分钟静默退出未根治** —— 0.1.11 已加健康快照、退出记录、
   崩溃栈、看门狗与 guardian 历史，先用真机数据定位；guardian 仍常驻兜底。
8. **未在干净机器上验证过安装包** —— 0.1.21 已本地构建
   `dist\room-watch-0.1.21-setup.exe`，但同机存在开发环境，仍需干净机器验证。

---

## J. 当前状态（2026-10-20）

- **分支**：`main` = `b63f8a2`（稳定版 0.1.12）；`beta` = `5b042b8`（0.1.20，
  已推送并发布预发布 `v0.1.20-beta`）；开发分支 `fix/0.1.21-settings-voice`
  （语音等身份结论、三页设置热更新、运行时长、Webhook 明文显示，
  `VERSION` = 0.1.21，尚未推送/发版）。
- **已完成**：M0–M5 主线、更新通道、身份等待窗口；0.1.21 修补真机反馈。
- **未做**：托盘（需 pystray + 真机）、M6 平台抽象、干净机器安装验证。
- **下一步**：真机验收 0.1.21 → 合回 `beta` → 稳定后升修订号再合 `main`、
  从 `main` 发 stable Release。
- **详细交接**：`docs/handoff/2026-10-19-session.md`（第 18 节为 0.1.21）；
  新会话入口 `CLAUDE.md`。
