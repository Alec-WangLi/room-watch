# room-watch · 房间进入抓拍

摄像头盯着房间，YOLO 确认「有人进来了」就抓一张带时间戳的照片、录一段 mp4、
本机语音播报、再推一条微信消息。全部在本机跑，视频不出机器。

[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

> 当前版本 **0.1.4**（`VERSION`，测试版）。桌面程序双击即用，命令行统一走 `rw`。
> 版本纪律：前两位是已批准基线（`VERSION-APPROVED`），未经所有者同意只动第三位。
> 结构说明见 [`docs/architecture.md`](docs/architecture.md)，协作约定见 [`AGENTS.md`](AGENTS.md)，
> 发版与 Release 规范见 [`docs/publishing.md`](docs/publishing.md)。
>
> 许可证 **AGPL-3.0**；第三方模型（YOLOv8 / YuNet / SFace）的来源与许可见 [`NOTICE`](NOTICE)。

## 为什么不是 Frigate / MotionEye

它们是出色的开源项目，但都假设你有一台 **Linux + Docker** 的常开主机，且摄像头走 RTSP。
在 Windows 上 Docker 跑在 WSL2 里，USB 摄像头无法直通。

而这里的需求很窄——「有人进来，拍一张，告诉我」。为它搬一整套 NVR，80% 的复杂度是白付的。
所以本方案是**约 4800 行 Python + YOLO**（含语音播报、人脸检测、本地相册、响度处理），
直接读本地摄像头。如果将来添了常开小主机或 RTSP 摄像头，再上 Frigate 更合适。

---

## 两种用法的关系

| 你想做的事 | 用哪个 |
|---|---|
| 打开窗口看状态、启停监控、翻抓拍 | **桌面程序** `dist\room-watch.exe` |
| 装完就想跑、或要写脚本 / 计划任务 | **命令行** `rw start` |
| 在手机 / 平板上看抓拍 | 相册 `rw gallery`（默认只有本机；`gallery.host=0.0.0.0` + `gallery.token` 后手机可访问） |

桌面程序只是一个「控制台」：它读状态文件、启停子进程，**自己不碰摄像头**。
关掉窗口不会停止监控，也不会影响正在录的像。

---

## 快速开始

### 第 0 步（新克隆必做一次）：启用提交前检查

```
scripts\setup-hooks.bat
```

双击运行，看到 `hooksPath : .githooks` 才算成功。`core.hooksPath` 是本地配置、
不随仓库分发，**不启用的话真实推送凭据可能被提交进 Git 历史且删不掉**。

### 方式一：桌面程序（推荐日常使用）

1. 双击 `dist\room-watch.exe`；
2. 首次使用先点「设置」填好摄像头、推送与相册；
3. 点「验证」确认摄像头和推送真的可用；
4. 点「启动监控」，状态变成 `● 在线`；
5. 点「打开相册」在浏览器里看抓拍；
6. 想放到桌面：右键运行 `scripts\install-shortcut.ps1`。

窗口里能做的事：启动 / 停止监控、打开相册、拍预览图、注册本人、**验证**、
**设置（八个页签）**、生成脱敏诊断包、查看最近事件、打开最新抓拍与日志目录。
状态每 2 秒刷新一次。

> **停止是「优雅」的**：界面先让监控自己收尾（当前 mp4 写完整、音轨合上、
> 音量还原），最多等 20 秒；只有超过这个时间才会问你要不要强制结束。
> 停止过程中两个按钮都锁住，状态显示「正在停止…（录像收尾中）」。
> 判活同时看心跳和进程号，所以停止后立刻就能再次「启动监控」。

「设置」窗口里能改的东西（改动写在 `config.local.yaml`，升级不丢）：

| 页签 | 内容 |
|---|---|
| 摄像头 | 本机编号 / 网络地址、测试摄像头、改回本机摄像头 |
| 推送 | 渠道勾选、各渠道凭据、录像推送、人员事件/报平安开关、**发送测试消息** |
| 相册 | 仅本机 / 局域网、密码、实际地址与手机 URL |
| 身份 | 本人模板状态、注册本人、用最新抓拍注册、清除模板、阈值与策略 |
| 录像 | 时长策略、本人在场策略、容量上限、保留天数与存储预警 |
| 心跳 | 外部死手开关 URL、间隔、状态、测试 |
| 自启 | 开机自启的开/关与状态（指向别的安装目录时会警示） |
| 声音 | 播报开关/文案/冷却、以及播报音量策略（默认**不碰系统音量**） |

> 声音页里的三档：`off` 完全不碰系统音量（默认）；`session` 只把本程序的
> 音轨拉到 100%（不动系统音量滑块）；`master` 才会改系统主音量。
> **录音走麦克风，和系统播放音量无关** —— 所以录到的声音不会因为选 `off` 变小。

### 方式二：命令行

```bash
./rw doctor            # 先自检：解释器、依赖、模型、目录、端口、钩子
./rw probe             # 拍一张预览图，确认角度（写到 var/media/preview.jpg）
./rw dry-run           # 试跑：只存档不推送，观察误报率
./rw start             # 后台启动：写 running 并交给 guardian 托管
./rw start --foreground  # 前台常驻，Ctrl+C 退出（调试用）
./rw stop              # 优雅停止：先暂停，再停相册与监控
```

> USB 摄像头只能在**用户登录会话**里采集（Windows 服务跑在 Session 0，
> 没有受支持的摄像头路径）。所以电脑需要保持登录：可以开自动登录后锁屏，
> 或者让家人账号保持登录。安装器/向导会注册登录时静默启动。

Windows 下把 `./rw` 换成 `rw`（`rw.cmd` 会自己找对解释器）。

### 交付给客户：单文件安装包（推荐）

```
# 构建机上装一次 Inno Setup（客户机不需要）
winget install -e --id JRSoftware.InnoSetup

# 先出免安装目录，再打成安装包
powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

产物 `dist\room-watch-<版本>-setup.exe`（142MB）。客户双击下一步即可：

- 装到 `%LocalAppData%\Programs\room-watch`，**不要管理员密码**；
- 建开始菜单快捷方式，可选桌面图标；
- 装完自动弹配置向导（摄像头 / webhook / 相册密码 / 开机自启）；
- 卸载在「设置 → 应用」里，会取消开机自启，**保留** `config.local.yaml` 与 `data\`
  （升级走的是旧卸载器，删配置等于每次升级都要重配）。

安装包是中文界面。Inno 官方包不带简体中文，语言文件在
`scripts\inno\ChineseSimplified.isl`（取自 issrc 的 Unofficial 目录）。

### 备选：免安装分发包（zip）

```
powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1
```

产出 `dist\room-watch-<版本>\` 和同名 zip（176MB，解压后 368MB）。
包里带了嵌入式 Python、运行依赖、ffmpeg 和模型，**客户机不需要装任何东西**：

1. 解压到任意目录（不要放 `C:\Program Files`，那里普通用户不可写）；
2. 双击 `配置.bat`，回答四个问题：摄像头、企业微信 webhook、相册密码、开机自启；
   向导会当场探测摄像头并发一条测试消息，失败会说明怎么改；
3. 双击 `启动.bat` 打开界面，点「启动监控」。

再次修改配置不用重装：重跑 `配置.bat`（或 `rw.cmd setup`），
已有的键会保留，只覆盖这次填的项。

> 打包脚本不会把本机的 `config.local.yaml` 带进去（里面有你的 webhook 和密码）。
> 改完源码只想换代码、不重装依赖：加 `-SkipDeps`，几秒完成。

### 重新打包桌面程序

```
右键 scripts\build-exe.ps1 → 使用 PowerShell 运行
```

脚本会用系统 Python 建 `.venv-gui`（自带 tkinter）并装 Pillow + PyInstaller，
产出 `dist\room-watch.exe`，最后自动跑一次 `--selftest` 验证。

---

## 全局命令

`rw --help` 看全部；每条命令都可以追加原模块参数（`--config` 等）。

| 命令 | 作用 |
|---|---|
| `rw start` | 后台启动监控，交给 guardian 托管（`--foreground` 前台调试） |
| `rw dry-run` | 抓拍存档但不推送 |
| `rw probe` | 拍预览图后退出 |
| `rw status` | 是否在线（读取时重算心跳） |
| `rw verify [--json] [--no-camera]` | 一次验证摄像头、推送与自检是否真的在工作 |
| `rw gallery [--host H] [--port 8765]` | 相册（非回环地址必须配 token） |
| `rw guard [--once\|--status\|--dry-run]` | 守护进程：监控 + 相册 |
| `rw enroll [--from IMG]` | 注册本人人脸 |
| `rw whoami IMG` | 判断某张图是不是本人 |
| `rw say` / `rw say-raw` / `rw ab` | 试听语音（处理后 / 原始 / 对比） |
| `rw volume-check` / `rw volume-set 70` | 音量诊断 / 设置主音量 |
| `rw stop` | 暂停并优雅停止相册与监控（先收尾，超时才强杀） |
| `rw config [--json\|--set 键=值\|--test]` | 读/改界面设置、发测试消息 |
| `rw autostart [--json\|--enable\|--disable]` | 开机自启（计划任务优先，失败回退 Run 键） |
| `rw identity [--json\|--clear]` | 查看/清除本人模板元信息 |
| `rw storage [--json]` | 磁盘占用、容量预测与最旧文件 |
| `rw bench-encode` | 编码基准（cv2 与 ffmpeg 硬件编码器，只测量） |
| `rw bench-detect` | 推理基准（只测量，不切换引擎） |
| `rw push` | 配置推送渠道 |
| `rw setup` | 首次配置向导（摄像头 / webhook / 相册密码 / 开机自启） |
| `rw push-video PATH` | 把一段录像发到企业微信（验证视频推送） |
| `rw test [--fast]` | 跑全部测试（`--fast` 跳过集成测试） |
| `rw doctor` | 环境自检 |
| `rw diag [--json]` | 生成脱敏诊断包（轮转日志/健康快照/退出记录/事件摘要/脱敏配置） |
| `rw update [--check\|--yes\|--force\|--json]` | 检查并安装更新（见「自动更新」） |
| `rw version` | 版本号 + 当前 git 提交 |
| `rw gui` | 打开桌面界面 |

---

## 目录结构（只列你可能会动的）

```
room-watch/
├─ rw / rw.cmd            # 全局命令
├─ config.yaml            # 所有可调参数
├─ roomwatch/             # 源码包（40 个模块，含 cli/paths/gui）
├─ tests/                 # 38 个测试文件，全部离线可跑
├─ scripts/               # 启动脚本、钩子、开机自启、打包
├─ models/  yolov8n.pt    # 权重（已入库，别删）
├─ data/captures/<日期>/   # 抓拍 jpg + 录像 mp4 + 音频 wav（隐私，不入库）
├─ data/identity/         # 本人人脸特征（隐私，不入库）
├─ var/logs/              # watcher / gallery / probe 日志
├─ var/state/             # status.json、守护状态与锁
├─ var/db/events.db       # 事件库
├─ var/media/             # 预览图等临时产物
└─ dist/room-watch.exe    # 桌面程序（打包产物，不入库）
```

规则：**只有 `roomwatch/` 放源码；`data/` 是隐私；`var/` 可以直接删**。

---

## 配置

全部在 `config.yaml`，每段都有注释。改完**必须重启监控**才生效
（配置只在启动时读一次）。

界面「设置」里保存的项会写到 `config.local.yaml`（与 `config.yaml` 深合并）。
**为什么不写主配置**：安装包升级会整体覆盖 `config.yaml`，写那里的设置下次更新就丢了。

几个最常调的：

| 键 | 默认 | 说明 |
|---|---|---|
| `camera.source` | 空 | 空=用 `camera.index`；也可填 `rtsp://…` 或视频文件 |
| `detector.engine` | auto | auto / onnx / torch；ONNX 不需要 torch |
| `gate.enabled` | true | 静止门控，空房间不推理 |
| `detector.conf` | 0.55 | 调高少误报，调低少漏报 |
| `detector.trigger_frames` | 3 | 连续几帧命中才算「进入」 |
| `snapshot.keep_days` | 30 | 按天清理 |
| `storage.max_total_gb` | 20 | 按体积清理。实测写入约 **65.6 MB/分钟**，20GB 只够约 5 小时录像 |
| `record.pre_roll_seconds` | 8 | 触发前预录秒数 |
| `record.codec` | avc1 | H.264，浏览器才放得动 |
| `identity.threshold` | 0.45 | 人脸相似度阈值 |
| `voice.require_face` | true | 看清脸才出声 |
| `voice.volume_mode` | `"off"` | `off` 不碰系统音量 / `session` 只提本程序音轨 / `master` 改主音量 |
| `notify.channel` | none | wxpusher / serverchan / wecom_bot |
| `notify.wecom_bot.send_video` | true | 录像也发（先压缩到 `video.max_mb`） |
| `gallery.token` | 空 | 相册访问密码；绑非回环地址时必填 |
| `notify.wecom_bot.video.max_mb` | 18 | 企业微信文件硬上限 20MB，压不到就只发文字 |

### 微信里能收到什么

| 渠道 | 文字 | 照片 | 录像 |
|---|---|---|---|
| `wecom_bot`（企业微信群机器人） | ✓ | ✓ base64 直传 | ✓ 转码后当文件发 |
| `wxpusher` / `serverchan` | ✓ | ✗ | ✗ |

只有企业微信机器人能把画面直接送到手机，且不经过任何公网图床 —— 照片不出本机。
录像走「后台队列 + ffmpeg 瘦身」：实测 99MB/86 秒压到 2.8MB，约 1 秒完成，不卡监控。
注意机器人只支持**内部群**，别把房间画面推进有同事的全员群。

### 推送凭据不要写在入库文件里

`config.yaml` 入库，四个凭据键必须是占位串。真实凭据放 **`config.local.yaml`**（已被忽略）：

```
rw push --channel wecom_bot --webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
```

这条命令只写 `config.local.yaml`（3 行左右：渠道 + 凭据），`config.yaml` 一个字都不动。
启动时 `roomwatch.config.load_config()` 会自动把同目录的 `config.local.yaml` **深合并**到主配置之上，
所以启动方式不用改（`rw start` / `rw guard` / 桌面快捷方式都照旧）。

比「复制一份完整配置」更好的地方：主配置改了自动跟随，不会出现「明明改了没生效」的漂移。

提交前钩子仍会拦真实凭据，命中时会打印行号、值和修法。

---

## 相册

```bash
rw gallery                 # http://127.0.0.1:8765/
```

只监听本机。页面顶部显示监控是否在线（同样按心跳重算），点缩略图播 mp4。
URL 路径固定是 `/captures/<日期>/<文件名>`（对应磁盘上的 `data/captures/`）。

想把相册暴露到手机，需要内网穿透（frp / Tailscale 等）。
**不要把 `--host` 改成 0.0.0.0 直接暴露公网**——那是你房间的照片。

---

## 人脸识别

```bash
rw enroll                   # 正对镜头自动采十几张
rw enroll --from 某张图.jpg  # 用现成照片注册
rw whoami 某张图.jpg         # 验证识别准不准
```

注册结果在 `data/identity/owner.npz`，换机器重新注册即可，不入库。
`identity.on_owner=skip` 表示认出你就不抓拍不推送；`unknown_policy=alert`
表示认不出照常告警（安全优先）。

三个坑（详见 AGENTS.md F 节）：

- 人脸检测是在 YOLO 人形框的裁剪图上跑的，关键点必须补回偏移再对齐，否则**静默认错人**；
- 阈值调太低会把别人认成你；
- 注册时别戴帽子、别逆光，否则模板质量差。

---

## 测试

```bash
rw test          # 11 个文件、261 项，约 90 秒
rw test --fast   # 跳过最慢的集成测试（约 55 秒）
```

全部离线：不联网、不推送、不读真实摄像头。`test_gallery.py` 会真起一个 HTTP 服务
（占用 8765 约 17 秒），正式相册在跑时先停掉或换端口。

---

## 排障

| 现象 | 先看哪里 |
|---|---|
| `ModuleNotFoundError: No module named 'cv2'` | 解释器选错了，跑 `rw doctor`；详见 AGENTS.md B 节 |
| 状态显示在线但其实没画面 | 不要信 `status.json` 里的 `stale`，用 `rw status` 重算 |
| 进程不知道怎么退出的 | 看 `var/state/last_exit.json` 与 `var/logs/watcher-fault.log`；`rw guard --status` 看重启历史 |
| 要反馈问题 | 先跑 `rw diag`，把生成的脱敏诊断包附上（不含房间图片与原始凭据） |
| 浏览器里视频拖不动进度条 | 相册的 Range 支持只在 206 响应下生效，看 `var/logs/gallery.log` |
| 录像 0 字节 / 放不出来 | 进程被硬杀，mp4 缺 moov atom，这段救不回来 |
| 日志里刷 `Failed to load OpenH264 library` | 噪音，实际由 Media Foundation 编码成功，可忽略 |
| 第二份监控起不来 | 摄像头独占，先停掉旧的（`rw status` 看 PID） |

已知限制：

- **本人在场时默认仍录像**：`record.owner_at_computer` 默认 `mute`（照录不推送）。
  想连录像一起停就显式改成 `skip`；容量上限与存储预测会兜底。
- **相册只按 PID + 命令行判活**：进程还在但 HTTP 卡死时，guardian 不会发现；
  这时看 `var/logs/gallery.log` 或手动重启相册。
- **暂停后需要显式启动**：`rw stop` / 界面「停止监控」会写 `paused`，
  之后必须 `rw start` / 界面「启动监控」才会恢复；guardian 不会自动拉起。

更多问题见 [FAQ](docs/faq.md)；数据去向与隐私边界见 [隐私说明](docs/privacy.md)。

原生崩溃转储（WER LocalDumps）默认不开启：它需要注册表写权限，普通安装不应
擅自修改系统设置。要抓 cv2 或驱动层崩溃时，按
[微软文档](https://learn.microsoft.com/windows/win32/wer/collecting-user-mode-dumps)
临时开启，把生成的 `.dmp` 与 `rw diag` 的 zip 一起提供。

---

## 版本与更新

`VERSION` 是唯一来源：`roomwatch.__version__` 读它，`pyproject.toml` 从
`__version__` 取，`rw version` 打印它，`tests/test_paths.py` 保证三者一致。

发版：改 `VERSION` → 在 `CHANGELOG.md` 顶部加一节 → 打 tag → 上传安装包与
`.sha256`（细节见 [`docs/publishing.md`](docs/publishing.md)）。

### 自动更新

界面右上角有「检查更新」：有新版时按钮会变成「有新版本 0.1.x」，
点一下确认即可更新（界面自动关闭 → 独立控制台里下载并安装 → 装完自动重开界面）。
界面启动时也会按 `update.check_interval_hours`（默认 24 小时）在后台静默查一次，
结果缓存在 `var/state/update.json`；**只提示，不自动安装**。

**更新通道**在「设置 → 更新」里选：

- `stable`（默认）：只收 GitHub 正式 Release，行为最稳。
- `beta`：连测试版预发布一起收，尝鲜用；同一版本同时有稳定版时优先稳定版。
  切换后旧检查缓存会被清掉，下次检查立即按新通道执行。

```bash
rw update --channel beta   # 临时按测试版通道检查/更新
```

命令行等价写法：

```bash
rw update            # 检查并在允许时直接安装
rw update --check    # 只看有没有新版，不下载
rw update --channel stable|beta  # 选择更新通道（也可写在 config.local.yaml）
rw update --yes      # 允许跨次版本（0.1.4 → 0.2.0）
rw update --force    # 允许降级（回滚排障用）
rw update --json     # 机器可读（一行 JSON，隐含 --check，界面用）
rw update --wait-exit 1234 --relaunch-gui   # 等界面退出再装、装完自动开界面
```

行为按版本纪律分流，不是「有新版就闷头装」：

| 远端版本 | 行为 |
|---|---|
| 同一 `主.次`，只前进修订号 | 自动下载、校验、安装、重启监控 |
| 跨次版本 / 跨主版本 | 打印更新说明，要求 `rw update --yes` |
| 比本地旧 | 默认拒绝 |

安装包必须提供 SHA256（`.sha256` 资产或说明里的 `sha256:`），校验不过直接拒绝安装。
GitHub 匿名 API 每小时 60 次，被打满时自动退回网页重定向取版本号，不占配额。

Linux / 源码部署不走这条链路（只对 Windows 安装包有效），更新用 `git pull`。

## 许可证

**AGPL-3.0**，全文见 [`LICENSE`](LICENSE)。

选它的直接原因是 `models/yolov8n.onnx` 来自 Ultralytics YOLOv8，本身即 AGPL-3.0；
主许可证与之一致，不存在「声明 MIT 却打包了 AGPL 权重」的矛盾。

如果你要闭源商用，两条路：向 Ultralytics 购买商业授权，或把检测模型换成
Apache-2.0 的（YOLOX-Nano / NanoDet-Plus），再按需调整本项目许可证。

第三方组件的来源与许可逐项列在 [`NOTICE`](NOTICE)。

