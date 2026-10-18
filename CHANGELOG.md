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
