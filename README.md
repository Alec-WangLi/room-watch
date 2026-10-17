# room-watch · 房间进入抓拍

摄像头盯着房间，YOLO 确认「有人进来了」就抓一张带时间戳的照片、录一段 mp4、
本机语音播报、再推一条微信消息。全部在本机跑，视频不出机器。

> 当前版本 **0.1.0**（`VERSION`）。桌面程序双击即用，命令行统一走 `rw`。
> 结构说明见 [`docs/architecture.md`](docs/architecture.md)，协作约定见 [`AGENTS.md`](AGENTS.md)。

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
| 在手机 / 平板上看抓拍 | 相册 `rw gallery`（本机 8765 端口） |

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
2. 点「启动监控」，状态变成 `● 在线`；
3. 点「打开相册」在浏览器里看抓拍；
4. 想放到桌面：右键运行 `scripts\install-shortcut.ps1`。

窗口里能做的事：启动 / 停止监控、打开相册、拍预览图、注册本人、
查看最近事件、打开最新抓拍与日志目录。状态每 2 秒刷新一次。

> 停止正在录像的监控会让当前 mp4 缺少收尾索引（不可播放），
> 界面在 `recording=true` 时会额外确认一次。

### 方式二：命令行

```bash
./rw doctor            # 先自检：解释器、依赖、模型、目录、端口、钩子
./rw probe             # 拍一张预览图，确认角度（写到 var/media/preview.jpg）
./rw dry-run           # 试跑：只存档不推送，观察误报率
./rw start             # 正式运行（前台常驻）
```

Windows 下把 `./rw` 换成 `rw`（`rw.cmd` 会自己找对解释器）。

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
| `rw start` | 启动监控（前台常驻，Ctrl+C 退出） |
| `rw dry-run` | 抓拍存档但不推送 |
| `rw probe` | 拍预览图后退出 |
| `rw status` | 是否在线（读取时重算心跳） |
| `rw gallery [--port 8765]` | 本地相册 |
| `rw guard [--once\|--status\|--dry-run]` | 守护进程 |
| `rw enroll [--from IMG]` | 注册本人人脸 |
| `rw whoami IMG` | 判断某张图是不是本人 |
| `rw say` / `rw say-raw` / `rw ab` | 试听语音（处理后 / 原始 / 对比） |
| `rw volume-check` / `rw volume-set 70` | 音量诊断 / 设置主音量 |
| `rw storage` | 磁盘占用与最旧文件 |
| `rw push` | 配置推送渠道 |
| `rw test [--fast]` | 跑全部测试（`--fast` 跳过集成测试） |
| `rw doctor` | 环境自检 |
| `rw version` | 版本号 + 当前 git 提交 |
| `rw gui` | 打开桌面界面 |

---

## 目录结构（只列你可能会动的）

```
room-watch/
├─ rw / rw.cmd            # 全局命令
├─ config.yaml            # 所有可调参数
├─ roomwatch/             # 源码包（13 个模块 + cli/paths/gui）
├─ tests/                 # 11 个测试文件，全部离线可跑
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

几个最常调的：

| 键 | 默认 | 说明 |
|---|---|---|
| `detector.conf` | 0.55 | 调高少误报，调低少漏报 |
| `detector.trigger_frames` | 3 | 连续几帧命中才算「进入」 |
| `snapshot.keep_days` | 30 | 按天清理 |
| `storage.max_total_gb` | 20 | 按体积清理。实测写入约 **65.6 MB/分钟**，20GB 只够约 5 小时录像 |
| `record.pre_roll_seconds` | 8 | 触发前预录秒数 |
| `record.codec` | avc1 | H.264，浏览器才放得动 |
| `identity.threshold` | 0.45 | 人脸相似度阈值 |
| `voice.require_face` | true | 看清脸才出声 |
| `notify.channel` | none | wxpusher / serverchan / wecom_bot |

### 推送凭据不要写在入库文件里

`config.yaml` 入库，四个凭据键必须是占位串。两种做法：

1. 复制一份 `config.local.yaml` 填真值（已被忽略），启动时 `rw start --config config.local.yaml`；
2. 直接改 `config.yaml`，提交前把值改回占位串。

提交前钩子会拦真实凭据，命中时会打印行号、值和三种修法。

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
| 浏览器里视频拖不动进度条 | 相册的 Range 支持只在 206 响应下生效，看 `var/logs/gallery.log` |
| 录像 0 字节 / 放不出来 | 进程被硬杀，mp4 缺 moov atom，这段救不回来 |
| 日志里刷 `Failed to load OpenH264 library` | 噪音，实际由 Media Foundation 编码成功，可忽略 |
| 第二份监控起不来 | 摄像头独占，先停掉旧的（`rw status` 看 PID） |

已知限制：

- **录像不知道「你是不是在电脑前」**：`behavior.skip_when_user_active` 只抑制推送，
  不抑制录像。按 65.6 MB/分钟算，全天办公能写数 GB。
- **gallery 没有自愈**：`guardian` 只管监控进程，相册挂了需要手动 `rw gallery`
  或用计划任务托管。
- 桌面程序的「停止监控」是强制终止（`taskkill`），不保证 mp4 收尾。

---

## 版本

`VERSION` 是唯一来源：`roomwatch.__version__` 读它，`pyproject.toml` 从
`__version__` 取，`rw version` 打印它，`tests/test_paths.py` 保证三者一致。

发版：改 `VERSION` → 在 `CHANGELOG.md` 顶部加一节 → `git tag vX.Y.Z`。
