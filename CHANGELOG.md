# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)：`主版本.次版本.修订号`。

- **版本号只有一个来源**：仓库根的 `VERSION` 文件。
  `roomwatch.__version__` 从它读取，`pyproject.toml` 从 `__version__` 读取，
  `rw version` 再打印它。三者不一致会被 `tests/test_paths.py` 拦下。
- **版本纪律（硬性，项目所有者定）**：前两位 `主版本.次版本` 是**已批准的基线**，
  记在 `VERSION-APPROVED` 里。**没有本人明确同意，只允许动第三位（修订号）**，
  也就是一直停在测试版序列（0.1.1 → 0.1.2 → …）。
  要进 `0.2.0` 这类正式版，必须先拿到同意，把 `VERSION-APPROVED` 改成新基线，
  再动 `VERSION` 的前两位。越界会被 `tests/test_paths.py::VersionTest` 判红。
- **发版动作**：改 `VERSION` → 在本文件顶部加一节。`git tag` 仍需本人批准。

---

## [0.1.4] - 2026-10-19

### 新增

- **自动更新**（`roomwatch/updater.py` + `rw update`）：从 GitHub Releases
  取最新版本，比对后按版本纪律处置 —— 同 `主.次` 的修订号前进自动安装，
  跨次版本/主版本必须 `--yes`，降级默认拒绝。下载后强制校验 SHA256，
  再停掉监控、静默安装、重新拉起。
- **API 限流回退**：匿名 GitHub API 只有 60 次/小时，实测会被打到 403；
  此时退回 `github.com/<repo>/releases/latest` 的 302 Location 取 tag，
  不消耗 API 配额，资产地址按 GitHub 固定路径拼。
- **开源配套**：`LICENSE`（AGPL-3.0）、`NOTICE`（逐项列出 YOLOv8 / YuNet /
  SFace 的来源与许可）、`docs/publishing.md`（发版与 Release 资产规范）、
  `docs/ci-tests.yml`（CI 定义；放进 `.github/workflows/` 即启用）。

### 变更

- **构建脚本不再写死本机路径**：`build-exe.ps1` 逐个 `import tkinter` 实测筛选
  解释器；`build-portable.ps1` 用 `py -3.13` 解析真实路径再回退 PATH 与默认安装目录。
  此前别人 clone 后必然构建失败。
- `AGENTS.md` 的 B.1/B.2/B.3 改成通用环境说明，删掉本机解释器对照表。
- `config.yaml` 新增 `update` 段（enabled / repo / allow_minor / require_checksum 等）。
- `pyproject.toml` 补 `license` 字段。

### 安全

- 测试里硬编码的真实企业微信 webhook key 换成全零占位值。
- 仓库历史用 `git-filter-repo` 重写：清除上述 key 与 `C:\Users\<本机>` 路径。
  **该 key 已按泄露处理，应在企业微信后台重新生成机器人。**
- 新增下载域名白名单（只允许 `github.com` / `objects.githubusercontent.com`）
  与 https 强制校验，见 `tests/test_updater.py::DownloadGuardTest`。

---

## [0.1.3] - 2026-10-19

### 新增

- **免安装分发包**（`scripts/build-portable.ps1`）：产出 `dist/room-watch-<版本>/`，
  内含嵌入式 Python 3.13、运行依赖、ffmpeg、模型与源码。客户解压后
  **不需要装 Python、不需要装 ffmpeg、不需要联网**，双击 `配置.bat` → `启动.bat` 即可。
  同目录再打一个 zip（约 150MB，解压后约 310MB）。
- **单文件安装包**（`scripts/build-installer.ps1` + `scripts/room-watch.iss`）：
  把上述目录打成 `dist/room-watch-<版本>-setup.exe`（142MB），双击即装，
  **装到 `%LocalAppData%\Programs\room-watch`，不需要管理员权限**。
  自动建开始菜单快捷方式、可选桌面图标，装完直接弹配置向导；
  卸载器会取消开机自启。中文界面用 `scripts/inno/ChineseSimplified.isl`
  （Inno 官方包里没有中文，取自 issrc 的 Unofficial 目录）。
  卸载保留 `config.local.yaml` 与 `data\`、`var\`——Inno 升级时先跑旧卸载器，
  删了就等于每次升级都要重配摄像头和 webhook。
- **首次配置向导**（`roomwatch/wizard.py`，命令 `rw setup`）：四个问题配好
  摄像头（编号 / `rtsp://` / 视频文件）、企业微信 webhook、相册密码、开机自启；
  写完立刻**探测摄像头**并发一条**测试消息**，失败时直接说清怎么改。
  只写 `config.local.yaml`，主配置的注释与占位串原样保留。
- **开机自启**（`rw setup --autostart`）：写 HKCU Run 注册表项 + 生成
  `start-monitor.vbs` 静默拉起守护进程，**不需要管理员权限**、不闪黑框。
  `rw setup --remove-autostart` 取消。
- `rw doctor` 增加「内嵌运行时」「ffmpeg」两项检查。

### 变更

- `paths.py` 支持分发布局：`find_python()` 优先用包内 `runtime/python/python.exe`，
  其次是本机已知环境；`PROJECT_ROOT` 从包位置推导，与当前工作目录无关。
- 配置里 `record.audio.ffmpeg` 写裸名 `ffmpeg` 时，若包内有 `tools/ffmpeg.exe`
  则自动改用它；显式写了绝对路径则原样尊重。
- `guardian` 新增 `--log PATH`：用 `pythonw` 静默启动时也有日志可查
  （此前 stderr 被丢弃，守护日志完全不可见）。
- 移除 `cli._PASSTHROUGH_MODULES`（定义了但从未被引用）。
- `rw doctor` 的「提交钩子」在非 git 目录（分发包）下改为「跳过」，
  不再对着客户报一条无意义的 WARN。
- 分发包多删一个 `win32comext`（pyttsx3 的 SAPI5 只走 `win32com`，
  实测删掉后仍能枚举 2 个语音）；`win32\lib\win32con.py` 保留。

### 实测

- 分发包体积：目录 367.9MB，zip 176.0MB；安装包 142.1MB（lzma2/max，编译约 51 秒）。
- **端到端验证过一遍**：`setup.exe /VERYSILENT` 装到临时目录 → 包内 `rw version`
  与 `room-watch.exe --selftest` 均为 0（窗口 620×560）；`rw doctor` 六项核心全 OK，
  解释器、ffmpeg 都指向安装目录自身 → 静默卸载 exitcode=0，
  **目录被完全删除、HKCU Run 无残留**。
- 包内 `import cv2` 正常，`rw version` / `rw doctor` 全部通过。
- 测试 20 个文件 400 项全绿（新增 `test_wizard.py` 25 项）。

---

## [0.1.2] - 2026-10-19

### 新增

- **RTSP / 网络摄像头接入**（`roomwatch/camera.py`）。`camera.source` 支持
  本机编号、`rtsp://`（含账号密码）、视频文件三种写法；设备走 dshow，
  网络流强制 FFmpeg + TCP + 5 秒超时。以前 `rtsp://` 会被 `int()` 抛 ValueError 直接退出。
- **断线重连退避**：网络流连续 3 次读帧失败即重连，等待 3→6→12…（封顶 60 秒）；
  本机设备仍是 10 次。文件源播完自动退出，不再空转。
- **静止门控**（`roomwatch/gate.py`）：帧差不过阈值就不推理。录像中或刚检测到人
  时自动旁路；空闲时每 `gate.force_every_seconds`（默认 3 秒）强制看一眼，
  避免「站着不动的人被判成人走了」。
- **人体检测改走 ONNX**（`roomwatch/onnx_detector.py`）：YOLOv8n → `cv2.dnn`，
  **不再需要 torch / ultralytics**。`detector.engine=auto` 时没有 ONNX 才退回 torch。
  后处理（letterbox / 解码 / NMS）自己实现，可离线单测。
- **相册鉴权**：`gallery.host/port/token`；绑定非回环地址时**没有 token 直接拒绝启动**。
  HTTP Basic（用户名随意，密码填 token），失败响应延迟 0.4 秒。

### 变更

- 主循环按「整轮耗时」控速，不再无条件 sleep，RTSP 下不会被压到 8fps。
- `rw enroll` 也走采集源解析，接 IP 摄像头时可以远程注册人脸。
- `rw doctor` 的模型检查改为认 `models/yolov8n.onnx`，`yolov8n.pt` 不再是必需项。
- `requirements.txt` 移除 ultralytics，运行依赖从约 1.2GB 降到约 160MB。

### 实测

- 同一张 720p 抓拍：torch 19ms（6 核）vs `cv2.dnn` 58.7ms（2 线程，约 0.6 核），
  框与置信度完全一致（400,5→1263,706，conf 0.93）。
- 12 秒视频源试跑：RSS 约 294MB（原 564MB），门控统计 `inferred 100 / skipped 20`。
- 测试 19 个文件 375 项全绿（新增 camera 15 / gate 8 / onnx_detector 13 / gallery_auth 18）。

---

## [0.1.1] - 2026-10-18

> 本节原记为 `0.2.0` 与 `0.2.1`（两节），但**次版本号未经批准**，按版本纪律
> 合并回退为修订号 `0.1.1`。功能内容不变。

### 新增

- **录像也能发到微信了**（`roomwatch/videopush.py`）。企业微信群机器人没有视频消息
  类型，只能发文件：ffmpeg 瘦身到 `video.max_mb`（默认 18，官方硬上限 20）
  → `upload_media` 换 `media_id` → 发 `file` 消息。
- **`rw push-video PATH`**：单独验证录像推送，不用等一次真实事件。
- **音轨合并完成回调**（`AudioRecorder.merge_async(on_done=...)`）：视频推送挂在合并
  之后，避免发出没有声音的哑巴视频。
- 配置项 `notify.wecom_bot.send_video` 与 `notify.wecom_bot.video.{max_mb,width,fps,audio}`。
- **`roomwatch/config.py`**：主配置深合并同目录 `config.local.yaml`（只写差异）。
- **版本纪律闸门**：`VERSION-APPROVED` + `VersionTest` 强制「未经批准不得升次版本号」。

### 修复

- **心跳推送从来没发成功过**。`maybe_heartbeat` 写的是 `notifier.send("监控在线", text)`，
  而 `send()` 的第二个参数是**图片字节**：正文被拿去做 base64，抛
  `a bytes-like object is required`，整条推送失败。切到企业微信后才暴露；
  wxpusher 时期更隐蔽——它只取第一个参数，于是永远只发出「监控在线」四个字。
  现在改为 `send(text)`，并在 `_wecom_bot` 里对非字节的第二参数做防御。
- **Server酱 渠道永久静默**。判定写成 `sendkey.startswith("SCT")`，
  而真实 SendKey 同样以 SCT 开头，于是真 key 被当成「没配置」直接跳过。
  改为只认占位符（`replace_me`）。
- **`rw push` 写入 webhook 的双嵌套 bug**（真机实测发现）。旧逻辑只替换
  `key=REPLACE_ME` 里的占位符，用户粘完整地址时配置里变成
  `?key=https://qyapi.../send?key=xxx`。文字消息接口校验宽松，照样「测试成功」；
  `upload_media` 严格校验 key，直接 `40058 invalid param 'key'` ——
  表现是「文字能收到、录像永远发不出」。
  新增 `normalize_wecom_webhook()` 统一剥 key，并改用整行替换。

### 变更

- **凭据迁到 `config.local.yaml`（只写差异）**：启动时自动深合并，
  `config.yaml` 保持占位串、一个字都不被写。不再需要 `rw start --config config.local.yaml`。
- `rw push` 只写 `config.local.yaml`，且按 key 合并——换渠道不会清掉其它渠道凭据。
- 图片与录像共用同一道推送闸门 `Watcher.notify_allowed()`（dry-run 不发、本人在电脑前不发）。

### 实测

- 企业微信群机器人（内部群）实收：文字 ✓、图片 ✓、录像 ✓。
- 本机 ffmpeg 9.0：36.2 MB/33.7 s → 0.52 MB（0.6 s）；94.7 MB/85.7 s → 2.84 MB（1.1 s）。
- 全部在后台线程串行执行，队列上限 8，满了丢弃，不拖慢检测主循环。

---

## [0.1.0] - 2026-10-18

第一个有明确版本号、统一入口和桌面界面的版本。功能与 0.0.x（未编号时期）一致，
差别全在工程结构上。

### 新增

- **桌面界面**（`roomwatch/gui.py`）：状态、启停、最新抓拍、最近事件、注册入口。
  界面是独立进程，只读状态文件与事件库，关掉窗口不影响监控。
- **单文件可执行程序**：`dist/room-watch.exe`（PyInstaller，内嵌 tkinter），
  双击即用，不需要记 Python 路径。
- **全局命令 `rw`**（`roomwatch/cli.py`，19 条命令）：
  `start / dry-run / probe / status / enroll / whoami / say / say-raw / ab /
  volume-check / volume-set / storage / gallery / guard / push / test / doctor /
  version / gui`。
- **`rw doctor`** 环境自检：解释器、依赖、模型、目录读写、人脸模板、提交钩子、相册端口。
- **`rw test [--fast]`**：一条命令跑全部测试，`--fast` 跳过最慢的集成测试。
- 版本体系：`VERSION` + `pyproject.toml` + `CHANGELOG.md` + git tag。

### 变更

- 13 个模块移入 `roomwatch/` 包；`logic.py` → `rules.py`，`faces.py` → `detector.py`。
- 8 个测试移入 `tests/`，新增 `test_cli.py` / `test_paths.py` / `test_gui_state.py`；
  合计 11 个文件、261 项（2026-10-18 实测全绿）。
- 路径基准统一到 `roomwatch/paths.py`：配置里的相对路径一律相对**项目根**，
  不再依赖「在哪个目录敲命令」，也修掉了三处 `dirname(__file__)` 造成的模型/模板定位歧义。
- 运行时数据分家：`captures/` → `data/captures/`，`identity/` → `data/identity/`；
  `events.db` / `status.json` / 日志 / 预览图 → `var/`。
- 启动脚本改走 `python -m roomwatch`，集中在 `scripts/`，并修正了搬目录后的相对路径。
- `setup-hooks.bat` 会先回到仓库根再执行，仍保持幂等与回读校验。

### 移除

- `.r4test/`（84 个 bat 编码实验文件，上一轮排查 cmd 编码问题时产生）。
  已打包备份到项目外 `_backups/room-watch/r4test-2026-10-18.zip` 后删除。
