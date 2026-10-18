# 项目结构（v0.1.2）

> 这一页回答「东西该放哪」。改代码前先看它，能避免 90% 的「放错地方」。
> 行为约定与踩坑记录在 `../AGENTS.md`，使用说明在 `../README.md`。

## 目录布局

```
room-watch/
├─ rw / rw.cmd              # 全局命令入口（bash / Windows）
├─ VERSION                  # 版本号唯一来源
├─ pyproject.toml           # 包元数据 + console_script: rw
├─ CHANGELOG.md             # 发版记录
├─ config.yaml              # 全部可调参数（入库，凭据用占位串）
├─ requirements.txt         # 本机依赖的实际版本
├─ roomwatch/               # 唯一的源码包
│  ├─ __init__.py           # __version__（读 VERSION）
│  ├─ __main__.py           # python -m roomwatch
│  ├─ cli.py                # 全局命令表与参数翻译
│  ├─ paths.py              # 项目根、运行时目录、解释器发现
│  ├─ watcher.py            # 主循环（采集→检测→抓拍→录像→播报→推送→清理）
│  ├─ rules.py              # 纯逻辑：连续帧确认、挑帧、冷却、录像收尾、清理
│  ├─ recorder.py           # 环形 JPEG 预录 + 后台线程写 H.264
│  ├─ audio_recorder.py     # 录音 + ffmpeg adelay 并音轨
│  ├─ audio_device.py       # 系统音量读写（pycaw 兜底）
│  ├─ detector.py           # 人脸检测（YuNet，Haar 兜底）
│  ├─ identity.py           # 人脸识别（SFace）+ 多帧投票
│  ├─ speaker.py            # SAPI5 语音合成与播放
│  ├─ loudness.py           # 响度增益（纯数学）
│  ├─ notifier.py           # 推送渠道（wxpusher / serverchan / wecom_bot / none）
│  ├─ gallery.py            # 本地相册 HTTP（标准库，自实现 Range）
│  ├─ guardian.py           # 守护进程（判活、退避、单实例锁）
│  ├─ setup_push.py         # 推送配置引导
│  └─ gui.py                # 桌面界面（Tkinter，独立进程）
├─ tests/                   # 11 个测试文件，全部可离线运行
├─ scripts/                 # 启动、钩子、开机自启、打包
├─ docs/                    # 本文件
├─ assets/                  # 图标
├─ models/                  # YuNet + SFace 权重（入库）
├─ yolov8n.pt               # YOLO 权重（入库）
├─ data/                    # 隐私数据，整目录不入库
│  ├─ captures/<日期>/       # 抓拍 jpg + 录像 mp4 + 音频 wav
│  └─ identity/owner.npz    # 本人人脸特征
└─ var/                     # 运行时可重建产物，整目录不入库
   ├─ logs/                 # watcher / gallery / probe / gui-selftest
   ├─ state/                # status.json / guardian_state.json / guardian.lock
   ├─ db/                   # events.db
   ├─ media/                # preview.jpg 等临时产物
   └─ cache/voice/          # 语音缓存
```

## 三条放置规则

1. **只有 `roomwatch/` 放源码。** 仓库根只留入口（`rw`）、配置、元数据与资产。
2. **`data/` 是隐私，`var/` 是可删。** 判断标准：删掉后会不会丢用户数据。
   抓拍、录像、人脸模板 → `data/`；日志、状态、缓存、预览图 → `var/`。
3. **路径一律经 `paths.resolve()`。** 配置里写相对路径，基准是项目根，
   与「在哪个目录敲命令」无关。不要在业务代码里写 `dirname(__file__)` 拼数据路径 ——
   那是包化前模型找不到、又静默不报错的原因。

## 新增一个全局命令的步骤

1. 在 `roomwatch/cli.py` 的 `COMMANDS` 里登记（`summary` + `module` + 是否需要取值）。
2. 若需要新参数，优先在被调模块的 argparse 里加，`cli.py` 只做翻译，不重复定义。
3. 在 `tests/test_cli.py` 加一条翻译断言（该给的值没给时是否报 `UsageError`）。
4. 在 `README.md` 的命令表与 `AGENTS.md` 的命令表各加一行。

## 依赖层次（谁可以 import 谁）

```
cli.py ──► watcher.py / gallery.py / guardian.py / setup_push.py / gui.py
                │
                ├─► rules.py        （纯函数，不依赖摄像头/网络/文件系统以外的东西）
                ├─► recorder.py / audio_recorder.py / audio_device.py
                ├─► detector.py / identity.py
                ├─► speaker.py / loudness.py
                └─► notifier.py
gui.py ──► paths.py + 标准库（刻意不 import cv2 / yaml，便于在无 cv2 的解释器里跑）
```

`rules.py` 是最底层：它不知道摄像头和网络存在，所以能 100% 离线单测。
新逻辑优先放这里，而不是塞进 `watcher.py`。

## 界面为什么是独立进程

监控进程必须「一直跑、不崩」；界面随时会被人关掉。放一个进程里，点错按钮就
会把录像一起带走。所以界面只做三件事：读 `var/state/status.json`（判活）、
读 `var/db/events.db`（列事件）、通过子进程启停监控与相册。

判活规则与 `rw status` 一致：**读取时重算** `now - last_frame_at`，
不信文件里存的 `stale` 字段（进程被强杀来不及改它）。
