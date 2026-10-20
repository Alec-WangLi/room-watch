# room-watch · 房间进入抓拍

摄像头盯着房间，YOLO 确认「有人进来了」就抓一张带时间戳的照片、录一段 mp4、
本机语音播报，再推一条消息。所有画面都在本机处理，不经过任何云端。

![version](https://img.shields.io/badge/version-0.2.2%20beta-orange)
![platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-blue)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

> **稳定版 0.2.0 · 最新测试版 0.2.2**
> Windows 10/11 x64，安装包双击即用；命令行统一走 `rw`。
> 架构说明见 [`docs/architecture.md`](docs/architecture.md)，协作约定见 [`AGENTS.md`](AGENTS.md)，
> 发版与 Release 规范见 [`docs/publishing.md`](docs/publishing.md)。
> 第三方模型（YOLOv8 / YuNet / SFace）的来源与许可见 [`NOTICE`](NOTICE)。

## 目录

- [它是什么](#它是什么)
- [亮点](#亮点)
- [为什么不是 Frigate / MotionEye](#为什么不是-frigate--motioneye)
- [快速开始](#快速开始)
- [桌面程序](#桌面程序)
- [命令行](#命令行)
- [配置](#配置)
- [推送与投递](#推送与投递)
- [相册](#相册)
- [人脸识别](#人脸识别)
- [自动更新](#自动更新)
- [测试与构建](#测试与构建)
- [排障](#排障)
- [已知限制](#已知限制)
- [版本与许可证](#版本与许可证)

---

## 它是什么

一次完整的「有人进门」是这样走完的：

1. **发现**：摄像头读到画面，静止门控先过滤掉没变化的帧，YOLO 连续几帧确认有人；
2. **取证**：抓拍一张带时间戳的 JPG，同时录一段同名 MP4（带预录，开头能看到进门那一刻）；
3. **辨认**：人脸识别区分本人和访客——本人静默处理，陌生人播报并推送；
4. **送达**：通知先写进本地发件箱，断网、重启后继续投递，不因网络抖动丢失；
5. **回看**：相册在手机或电脑浏览器里按日期翻看照片和录像。

监控、相册和守护进程都在本机运行；只有你显式配置的推送渠道会收到消息。

## 亮点

- **本机处理**：照片、录像、人脸特征都不上传；录像推送也只在你自己配置的渠道内发送。
- **事件取证完整**：预录、时间戳、音轨合并、分段续录，片段和照片同名配对。
- **稳定优先**：guardian 同时守护监控与相册，退出有退出码和原因，恢复会通知。
- **通知不丢**：SQLite 持久化发件箱、指数退避、多渠道扇出、容量与过期策略。
- **整机失联可发现**：外部心跳由 guardian 定时 ping，断电/断网由外部服务提醒。
- **省资源**：空房间不推理；0.2.2 起本人坐在电脑前时自动降低推理频率。
- **装完即用**：安装包、自动更新（stable / beta 双通道）、脱敏诊断包。

---

## 为什么不是 Frigate / MotionEye

它们是出色的开源项目，但都假设你有一台 **Linux + Docker** 的常开主机，且摄像头走 RTSP。
在 Windows 上 Docker 跑在 WSL2 里，USB 摄像头无法直通。

而这里的需求很窄——「有人进来，拍一张，告诉我」。为它搬一整套 NVR，80% 的复杂度是白付的。
所以本方案是一个专注的 Python 项目，直接读本地摄像头；如果将来添了常开小主机或
RTSP 摄像头，再上 Frigate 更合适。

---

## 快速开始

### 方式一：安装包（推荐日常使用）

1. 从 [Releases](https://github.com/Alec-WangLi/room-watch/releases) 下载最新的
   `room-watch-<版本>-setup.exe`；
2. 双击安装，装到 `%LocalAppData%\Programs\room-watch`，**不需要管理员权限**；
3. 首次打开界面后进入「设置」，填好摄像头、推送和相册；
4. 点「验证」确认摄像头和推送真的可用；
5. 点「启动监控」，状态变成 `● 在线`。

安装器会建开始菜单快捷方式，可选桌面图标；开机自启在「设置 → 自启」里开启
（安装器不会替你勾选，避免和向导打架）。
卸载在「设置 → 应用」里，会取消开机自启，但**保留** `config.local.yaml` 与 `data\`
（升级走的是旧卸载器，删配置等于每次升级都要重配）。

> 安装包是中文界面。Inno 官方包不带简体中文，语言文件在
> `scripts\inno\ChineseSimplified.isl`（取自 issrc 的 Unofficial 目录）。

### 方式二：免安装 zip

适合不想安装、或要放到 U 盘里带走的情况：

1. 下载 `room-watch-<版本>.zip`，解压到任意可写目录
   （不要放 `C:\Program Files`，普通用户不可写）；
2. 双击 `配置.bat`，回答四个问题：摄像头、企业微信 webhook、相册密码、开机自启；
   向导会当场探测摄像头并发一条测试消息；
3. 双击 `启动.bat` 打开界面，点「启动监控」。

包里带了嵌入式 Python、运行依赖、ffmpeg 和模型，**客户机不需要装任何东西**。
再次修改配置不用重装：重跑 `配置.bat`（或 `rw.cmd setup`）。

> 打包脚本不会把本机的 `config.local.yaml` 带进去（里面有 webhook 和密码）。
> 改完源码只想换代码、不重装依赖：给 `build-portable.ps1` 加 `-SkipDeps`。

### 方式三：源码运行

```bash
# 新克隆先启用提交前钩子（防止凭据进 Git 历史）
scripts\setup-hooks.bat

# 建议用带 cv2 / tkinter 的 Python 3.10+
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

rw doctor        # 先自检：解释器、依赖、模型、目录、端口、钩子
rw probe         # 拍一张预览图，确认角度
rw dry-run       # 试跑：只存档不推送，观察误报率
rw start         # 后台启动，交给 guardian 托管
```

Windows 下把 `./rw` 换成 `rw`（`rw.cmd` 会自己找对解释器）。

> **电脑需要保持登录。** USB 摄像头只能在用户登录会话里采集（Windows 服务跑在
> Session 0，没有受支持的摄像头路径）。可以开自动登录后锁屏，或让家人账号保持登录；
> 安装器/向导会注册登录时静默启动。

---

## 桌面程序

桌面程序是一个「控制台」：它读状态文件、启停子进程，**自己不碰摄像头**。
关掉窗口不会停止监控，也不会影响正在录的像。状态每 2 秒刷新一次。

窗口里能做的事：启动 / 停止监控、打开相册、拍预览图、注册本人、验证、设置、
生成脱敏诊断包、查看最近事件、打开最新抓拍与日志目录。

> **停止是「优雅」的**：界面先让监控自己收尾（当前 mp4 写完整、音轨合上、
> 音量还原），最多等 20 秒；只有超时才问你要不要强制结束。停止过程中两个按钮
> 都锁住，状态显示「正在停止…（录像收尾中）」。

「设置」窗口共九个页签，改动的项写到 `config.local.yaml`，升级不丢：

| 页签 | 内容 |
|---|---|
| 摄像头 | 本机编号 / 网络地址、测试摄像头、改回本机摄像头 |
| 推送 | 渠道勾选、各渠道凭据、录像推送、人员事件/报平安开关、发送测试消息 |
| 相册 | 仅本机 / 局域网、密码、实际地址与手机 URL |
| 身份 | 本人模板状态、注册本人、用最新抓拍注册、清除模板、阈值与策略 |
| 录像 | 时长策略、本人在场策略、容量上限、保留天数与存储预警 |
| 心跳 | 外部死手开关 URL、间隔、状态、测试 |
| 自启 | 开机自启的开/关与状态（指向别的安装目录时会警示） |
| 声音 | 播报开关/文案/冷却，以及播报音量策略（默认**不碰系统音量**） |
| 更新 | stable / beta 更新通道、检查间隔、手动检查 |

> 声音页有三档：`off` 完全不碰系统音量（默认）；`session` 只把本程序的音轨
> 拉到 100%（不动系统音量滑块）；`master` 才会改系统主音量。
> **录音走麦克风，和系统播放音量无关**——选 `off` 不会让录到的声音变小。

---

## 命令行

`rw --help` 看全部；每条命令都可以追加原模块参数（`--config` 等）。
最常用的几条：

```bash
rw doctor              # 环境自检
rw status              # 是否在线（读取时重算心跳）
rw verify              # 一次验证摄像头、推送与自检是否真的在工作
rw start / rw stop     # 后台启动 / 优雅停止（同时管相册）
rw gallery             # 打开本地相册（默认 127.0.0.1:8765）
rw diag                # 生成脱敏诊断包
```

### 运行与状态

| 命令 | 作用 |
|---|---|
| `rw start` | 后台启动监控，交给 guardian 托管（`--foreground` 前台调试） |
| `rw stop` | 暂停并优雅停止相册与监控（先收尾，超时才强杀） |
| `rw status` | 是否在线（读取时重算心跳） |
| `rw dry-run` | 抓拍存档但不推送 |
| `rw probe` | 拍预览图后退出 |
| `rw verify [--json] [--no-camera]` | 一次验证摄像头、推送与自检 |
| `rw gui` | 打开桌面界面 |

### 配置与身份

| 命令 | 作用 |
|---|---|
| `rw setup` | 首次配置向导（摄像头 / webhook / 相册密码 / 开机自启） |
| `rw config [--json\|--set 键=值\|--test]` | 读/改界面设置、发测试消息 |
| `rw enroll [--from IMG]` | 注册本人人脸 |
| `rw whoami IMG` | 判断某张图是不是本人 |
| `rw identity [--json\|--clear]` | 查看/清除本人模板元信息 |
| `rw push` | 配置推送渠道 |
| `rw autostart [--json\|--enable\|--disable]` | 开机自启（计划任务优先，回退 Run 键） |
| `rw heartbeat --test` | 测试外部心跳 URL |

### 维护与诊断

| 命令 | 作用 |
|---|---|
| `rw doctor` | 环境自检 |
| `rw diag [--json]` | 脱敏诊断包（轮转日志/健康快照/退出记录/事件摘要/脱敏配置） |
| `rw storage [--json]` | 磁盘占用、容量预测与最旧文件 |
| `rw gallery [--host H] [--port 8765]` | 相册（非回环地址必须配 token） |
| `rw guard [--once\|--status\|--dry-run]` | 守护进程：监控 + 相册 |
| `rw update [--check\|--yes\|--force\|--json]` | 检查并安装更新（见「自动更新」） |
| `rw test [--fast]` | 跑全部测试（`--fast` 跳过集成测试） |
| `rw version` | 版本号 + 当前 git 提交 |

### 基准与验证

| 命令 | 作用 |
|---|---|
| `rw bench [--seconds N] [--json]` | 采样运行中监控的 CPU / RSS，不占摄像头（0.2.2+） |
| `rw bench-encode` | 编码基准（cv2 与 ffmpeg 硬件编码器，只测量） |
| `rw bench-detect [--engine cv2,onnxruntime]` | 推理基准与检测一致性对比（只测量，0.2.2+） |
| `rw say` / `rw say-raw` / `rw ab` | 试听语音（处理后 / 原始 / 对比） |
| `rw volume-check` / `rw volume-set 70` | 音量诊断 / 设置主音量 |
| `rw push-video PATH` | 把一段录像发到企业微信（验证视频推送） |

---

## 配置

全部在 `config.yaml`，每段都有注释。改完一般需要重启监控才生效
（0.1.21 起「更新 / 自启 / 声音」三页支持热更新）。

界面「设置」里保存的项会写到 `config.local.yaml`（与 `config.yaml` 深合并）。
**为什么不写主配置**：安装包升级会整体覆盖 `config.yaml`，写那里的设置下次更新就丢了。

几个最常调的：

| 键 | 默认 | 说明 |
|---|---|---|
| `camera.source` | 空 | 空=用 `camera.index`；也可填 `rtsp://…` 或视频文件 |
| `detector.engine` | auto | auto / onnx / torch；onnxruntime 为可选后端（不随包分发） |
| `detector.conf` | 0.55 | 调高少误报，调低少漏报 |
| `detector.trigger_frames` | 3 | 连续几帧命中才算「进入」 |
| `gate.enabled` | true | 静止门控，空房间不推理 |
| `gate.owner_interval_seconds` | 0.5 | 本人在场且未录像时的推理间隔；0 = 关闭 |
| `snapshot.keep_days` | 30 | 按天清理 |
| `storage.max_total_gb` | 20 | 按体积清理；实际写入量看 `rw storage` 的预测 |
| `record.encoder` | auto | auto / cv2 / ffmpeg；auto 为 h264_mf → h264_nvenc → cv2（0.2.2+） |
| `record.ffmpeg_encoder` | auto | auto / h264_mf / h264_nvenc / libx264（0.2.2+） |
| `record.pre_roll_seconds` | 8 | 触发前预录秒数 |
| `identity.threshold` | 0.45 | 人脸相似度阈值 |
| `voice.require_face` | true | 看清脸才出声 |
| `voice.volume_mode` | `"off"` | `off` 不碰系统音量 / `session` 只提本程序音轨 / `master` 改主音量 |
| `notify.channels` | `[]` | 渠道列表：wecom_bot / wxpusher / serverchan / webhook |
| `notify.wecom_bot.send_video` | true | 录像也发（先压缩到 `video.max_mb`） |
| `gallery.token` | 空 | 相册访问密码；绑非回环地址时必填 |

> 录像默认走 ffmpeg 硬件编码（0.2.2 起）。`h264_mf` 在 Windows 10/11 上通常可用；
> 不可用时按 `h264_nvenc` → cv2 avc1 回退。`status.json` 里的 `video_encoder`
> 和 `video_encoder_error` 能看出这一段实际用了什么、为什么回退。
> `libx264` 只允许显式配置，不参与 auto。

---

## 推送与投递

### 微信里能收到什么

| 渠道 | 文字 | 照片 | 录像 |
|---|---|---|---|
| `wecom_bot`（企业微信群机器人） | ✓ | ✓ base64 直传 | ✓ 转码后当文件发 |
| `wxpusher` / `serverchan` | ✓ | ✗ | ✗ |
| 通用 `webhook` | ✓ | ✓（小图内联 base64） | ✗ |

只有企业微信机器人能把画面直接送到手机，且不经过任何公网图床——照片不出本机。
录像走「后台队列 + ffmpeg 瘦身」：实测 99MB/86 秒压到 2.8MB，约 1 秒完成，不卡监控。
注意机器人只支持**内部群**，别把房间画面推进有同事的全员群。

### 凭据不要写进入库文件

`config.yaml` 入库，四个凭据键必须是占位串。真实凭据放 **`config.local.yaml`**（已被忽略）：

```bash
rw push --channel wecom_bot --webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
```

这条命令只写 `config.local.yaml`，`config.yaml` 一个字都不动。启动时
`roomwatch.config.load_config()` 会把同目录的 `config.local.yaml` **深合并**到主配置之上，
所以 `rw start` / `rw guard` / 桌面快捷方式都不用改启动方式。

提交前钩子仍会拦真实凭据，命中时打印行号、值和修法。

### 投递与心跳

- 通知先落盘到 `var/db/outbox.db`，后台 worker 发送；断网/重启后继续投递。
- 网络错误、超时、5xx、429 按退避重试；401/403 等永久失败不再无限重试。
- `monitor.heartbeat_url` 由 guardian 定时 ping，整机断电/断网由外部服务提醒。
- 渠道健康、待发送数量、最近成功时间都能在界面与诊断包里看到。

---

## 相册

```bash
rw gallery                 # http://127.0.0.1:8765/
```

只监听本机。页面顶部显示监控是否在线（按心跳重算），点缩略图播 mp4。
URL 路径固定是 `/captures/<日期>/<文件名>`（对应磁盘上的 `data/captures/`）。

想把相册暴露到手机，需要内网穿透（frp / Tailscale 等），
或者在设置里改为局域网监听并设置 **12 位以上**密码。
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

三个坑（详见 `AGENTS.md` F 节）：

- 人脸检测是在 YOLO 人形框的裁剪图上跑的，关键点必须补回偏移再对齐，否则**静默认错人**；
- 阈值调太低会把别人认成你；
- 注册时别戴帽子、别逆光，否则模板质量差。

---

## 自动更新

界面右上角有「检查更新」：有新版时按钮变成「有新版本 0.2.x」，
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

---

## 测试与构建

```bash
rw test          # 41 个测试文件、约 960 项，约 2–3 分钟
rw test --fast   # 跳过最慢的集成测试
```

全部离线：不联网、不推送、不读真实摄像头。`test_gallery.py` 会真起一个 HTTP 服务
（占用 8765 约 17 秒），正式相册在跑时先停掉或换端口。

构建（构建机上装一次 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，客户机不需要）：

```powershell
# 1. 桌面程序（PyInstaller，自动建 .venv-gui 并跑 --selftest）
powershell -ExecutionPolicy Bypass -File scripts\build-exe.ps1

# 2. 免安装目录 + zip（加 -SkipDeps 只换源码，几秒完成）
powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1

# 3. 单文件安装包
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

产物：

| 产物 | 体积 |
|---|---|
| `dist\room-watch-<版本>-setup.exe` | 约 143 MB |
| `dist\room-watch-<版本>.zip` | 约 177 MB |
| 解压后的便携目录 | 约 369 MB |

### 项目结构

```text
room-watch/
├─ rw / rw.cmd             # 全局命令
├─ config.yaml             # 所有可调参数
├─ roomwatch/              # 源码包（检测、录像、守护、界面、CLI）
├─ tests/                  # 41 个测试文件，全部离线可跑
├─ scripts/                # 启动、钩子、自启、打包
├─ models/  yolov8n.pt     # 权重（已入库，别删）
├─ data/captures/<日期>/   # 抓拍 jpg + 录像 mp4 + 音频 wav（隐私，不入库）
├─ data/identity/          # 本人人脸特征（隐私，不入库）
├─ var/logs/  var/state/   # 日志、状态、事件库（可重建）
└─ dist/                   # 打包产物（不入库）
```

规则：**只有 `roomwatch/` 放源码；`data/` 是隐私；`var/` 可以直接删**。

---

## 排障

| 现象 | 先看哪里 |
|---|---|
| `ModuleNotFoundError: No module named 'cv2'` | 解释器选错了，跑 `rw doctor`；详见 `AGENTS.md` B 节 |
| 状态显示在线但其实没画面 | 不要信 `status.json` 里的 `stale`，用 `rw status` 重算 |
| 进程不知道怎么退出的 | 看 `var/state/last_exit.json` 与 `var/logs/watcher-fault.log`；`rw guard --status` 看重启历史 |
| 录像体积异常大 / 编码器回退 | 看 `status.json` 的 `video_encoder` / `video_encoder_error`；`rw doctor` 检查 ffmpeg |
| 要反馈问题 | 先跑 `rw diag`，把生成的脱敏诊断包附上（不含房间图片与原始凭据） |
| 浏览器里视频拖不动进度条 | 相册的 Range 支持只在 206 响应下生效，看 `var/logs/gallery.log` |
| 录像 0 字节 / 放不出来 | 进程被硬杀，mp4 缺 moov atom，这段救不回来 |
| 日志里刷 `Failed to load OpenH264 library` | 噪音，实际由 Media Foundation 编码成功，可忽略 |
| 第二份监控起不来 | 摄像头独占，先停掉旧的（`rw status` 看 PID） |

原生崩溃转储（WER LocalDumps）默认不开启：它需要注册表写权限，普通安装不应
擅自修改系统设置。要抓 cv2 或驱动层崩溃时，按
[微软文档](https://learn.microsoft.com/windows/win32/wer/collecting-user-mode-dumps)
临时开启，把生成的 `.dmp` 与 `rw diag` 的 zip 一起提供。

更多问题见 [FAQ](docs/faq.md)；数据去向与隐私边界见 [隐私说明](docs/privacy.md)。

---

## 已知限制

- **本人在场时默认仍录像**：`record.owner_at_computer` 默认 `mute`（照录不推送）。
  想连录像一起停就显式改成 `skip`；容量上限与存储预测会兜底。
- **相册按 PID + `/healthz` 判活**：进程在但 HTTP 连续两次无响应时，guardian 会重启
  相册并推送通知；实际端口从 `var/state/gallery.json` 读取，8765 被占用后自动换端口。
- **暂停后需要显式启动**：`rw stop` / 界面「停止监控」会写 `paused`，
  之后必须 `rw start` / 界面「启动监控」才会恢复；guardian 不会自动拉起。
- **桌面需要保持登录**（见「快速开始」）；Windows 服务模式不支持 USB 摄像头采集。
- **托盘图标尚未实现**：当前通过桌面窗口查看状态，监控本身不依赖窗口。

---

## 版本与许可证

`VERSION` 是唯一来源：`roomwatch.__version__` 读它，`pyproject.toml` 从
`__version__` 取，`rw version` 打印它，`tests/test_paths.py` 保证三者一致。
版本纪律：前两位是已批准基线（`VERSION-APPROVED`），未经所有者同意只动第三位。

发版：改 `VERSION` → 在 `CHANGELOG.md` 顶部加一节 → 打 tag → 上传安装包与
`.sha256`（细节见 [`docs/publishing.md`](docs/publishing.md)）。

**AGPL-3.0**，全文见 [`LICENSE`](LICENSE)。选它的直接原因是
`models/yolov8n.onnx` 来自 Ultralytics YOLOv8，本身即 AGPL-3.0；
主许可证与之一致，不存在「声明 MIT 却打包了 AGPL 权重」的矛盾。

如果你要闭源商用，两条路：向 Ultralytics 购买商业授权，或把检测模型换成
Apache-2.0 的（YOLOX-Nano / NanoDet-Plus），再按需调整本项目许可证。
第三方组件的来源与许可逐项列在 [`NOTICE`](NOTICE)。
