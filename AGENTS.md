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
| `roomwatch/guardian.py` | 守护进程（判活/退避/单实例锁） | |
| `roomwatch/setup_push.py` | 推送配置引导 | |
| `roomwatch/gui.py` | 桌面界面 | 独立进程，不 import cv2 |
| `roomwatch/cli.py` | 全局命令表与参数翻译 | 新增命令先改这里 |
| `roomwatch/paths.py` | 项目根 / 运行时目录 / 解释器发现 | 路径基准的唯一来源 |
| `roomwatch/config.py` | 配置加载：主配置 + `config.local.yaml` 深合并 + 路径归一 | 别在别处直接 `yaml.safe_load(config.yaml)` |

---

## B. 环境与解释器（最容易踩的一条）

### B.1 主环境（跑监控）

```
C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

Python 3.13.14，已装 cv2 5.0.0 / numpy 2.5.4 / ultralytics 8.4.178 / PyYAML / pyttsx3 /
sounddevice / psutil / pycaw。

本机 PATH 上还有**另外三个** python，全都跑不了本项目（缺 cv2）：

| 解释器 | 版本 | 用途 |
|---|---|---|
| `...\binaries\python\versions\3.13.12\python.exe` | 3.13.14（目录名是 3.13.12） | git-bash 敲 `python` 默认解析到它，最常踩 |
| `...\AppData\Local\Programs\Python\Python313\python.exe` | 3.13.13 | **有 tkinter 8.6，没 cv2**：桌面界面与打包用它 |
| `...\AppData\Local\Python\bin\python.exe` | **3.14.5** | 看到 3.14.5 就知道选错了 |

验证：`rw doctor`（会打印解释器、依赖、模型、目录、端口、钩子）。

### B.2 界面环境（打 exe）

`scripts\build-exe.ps1` 用系统 Python 3.13.13 建 `.venv-gui`（自带 tkinter），
只装 Pillow + PyInstaller。产物 `dist\room-watch.exe`（单文件，内嵌 tkinter）。
**不要试图用主环境打 GUI**：它没有 tkinter，PyInstaller 会失败。

### B.3 `rw` 如何选解释器

`paths.find_python()` 顺序：`ROOMWATCH_PYTHON` →（frozen 时）已知路径 → 当前解释器 → `python`。
`rw.cmd` 在 Windows 上先试 `%USERPROFILE%\.workbuddy\...\envs\default`，
所以双击和命令行都不需要配 PATH。

---

## C. 全局命令

统一入口 `rw`（Windows 用根目录 `rw.cmd`，bash 用根目录 `rw`，等价 `python -m roomwatch`）。
命令表在 `cli.py` 的 `COMMANDS`，**新增功能先在那里登记**，否则 `rw --help` 里看不到。

| 目的 | 命令 |
|---|---|
| 环境自检 | `rw doctor` |
| 拍预览图 | `rw probe` |
| 试跑（不推送） | `rw dry-run` |
| 正式运行 | `rw start` |
| 看是否在线 | `rw status` |
| 注册人脸 | `rw enroll` / `rw enroll --from IMG` |
| 验证识别 | `rw whoami IMG` |
| 试听语音 | `rw say` / `rw say-raw` / `rw ab` |
| 音量 | `rw volume-check` / `rw volume-set 70` |
| 磁盘占用 | `rw storage` |
| 相册 | `rw gallery [--port 9000]` |
| 守护 | `rw guard [--once\|--status\|--dry-run]` |
| 推送配置 | `rw push` |
| 验证录像推送 | `rw push-video PATH`（需 webhook 已配好） |
| 测试 | `rw test [--fast]` |
| 版本 | `rw version`（带 git 短哈希） |
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
| `var/logs/` | watcher / gallery / probe / gui-selftest | 否 |
| `var/state/` | status.json / guardian_state.json / guardian.lock | 否 |
| `var/db/events.db` | 事件库 | 否 |
| `var/media/` | preview.jpg 等临时产物 | 否 |
| `var/cache/voice/` | 语音缓存（可重建） | 否 |
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

19 个文件、375 项，全部可离线运行（`tests/` 下，均带 `unittest.main()`）：

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

全量：`rw test`（约 90 秒）；`rw test --fast` 跳过集成测试。

> 2026-10-19 v0.1.2 实测：19 个文件全部 `OK`，共 375 项。

---

## F. 踩过的坑 / 硬约束

每条都是真实事故，按「现象 → 原因 → 正解」记录。

### F.1 摄像头同时只能被一个进程占用；改完代码必须重启

- **现象**：第二份监控打不开摄像头；改完 `config.yaml` 后运行中的进程行为不变。
- **原因**：设备独占；配置只在 `Watcher.__init__` 里读一次，不热加载。
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

### F.4 判断是否活着必须重算心跳

- **现象**：进程被强杀后，`status.json` 里的 `stale` 仍是 `false`，把死的报成在线。
- **原因**：进程被杀前来不及再写状态。
- **正解**：在读取那一刻重算 `now - last_frame_at > heartbeat.stale_after_seconds`。
  `rw status`、相册页、桌面界面都按这个规则实现（`gui.read_state()`）。

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

---

## I. 已知待办（含为何暂缓）

1. **gallery 的长期守护方案未定** —— 后台任务方式会被系统回收；`guardian.py` 目前只守护监控。
   候选：Windows 计划任务 / 让 `start-guardian.bat` 同时拉起两个服务。
   **暂缓原因**：需要用户确认机器安全策略（WMI 与 `Start-Process` 都被拦）。
2. **录像在本人坐在电脑前时仍会录** —— `behavior.skip_when_user_active` 只抑制推送。
   实测约 65.6 MB/分钟，长期会吃满盘。**暂缓原因**：这是产品决策，需用户拍板。
3. **桌面的「停止监控」不优雅** —— 用 `taskkill /F`，不保证 mp4 收尾。
   可改成向进程发关闭信号并等待收尾（`watcher` 已有 KeyboardInterrupt 收尾路径）。
4. **~~`install-autostart.ps1` 硬编码绝对路径~~（已在 v0.1.0 修掉）** ——
   现在用 `$PSScriptRoot` 推导项目根，参数改为 `-m roomwatch guard --once`，并补了 BOM。
   保留这条记录，避免有人再「优化」回硬编码。
