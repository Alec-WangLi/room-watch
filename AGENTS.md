# room-watch · 项目约定与协作习惯

> 读者：未来的 AI 助手，以及用户本人。
> 目标：开新对话时先读这一页，就能直接干活，不用重新摸一遍。
> 本文所有命令、路径、配置键名、行号，**都在 2026-10-18（首次 git 提交时）逐条核对过源码**。
> 若你发现某条与代码不一致，说明代码已经变了 —— 请以代码为准，并顺手改掉这一行。

---

## A. 项目定位与数据流

**一句话**：电脑摄像头盯着房间，YOLO 确认「有人进来了」就抓一张带时间戳的照片、同时录一段 mp4、
本机语音播报、再推一条微信消息。全部在本机跑，视频不出机器。

### 主链路（每一步后面的括号是出处）

```
摄像头
  camera.index / camera.backend=dshow / 1280x720 / 10fps        config.yaml:6-11
    ↓
YOLO 人形检测
  yolov8n.pt，conf=0.55，imgsz=640                              config.yaml:13-16
    ↓
连续帧确认「真的有人」
  detector.trigger_frames=3 帧连续命中才算进入                  config.yaml:17, logic.py:16-21
    ↓
挑最好的一帧
  再看 detector.capture_window=8 帧，按 frame_score 取最大最居中  config.yaml:18, logic.py:125
    ↓
落盘 + 开录
  captures/日期/HHMMSS.jpg（画框+时间戳）
  同名 .mp4，avc1(H.264)，带 8 秒预录                            config.yaml:21-56
    ↓
人脸检测（在 YOLO 框内找脸）
  YuNet ONNX，每 3 帧一次，score≥0.7，返回 15 维行（含关键点）    config.yaml:78-87, faces.py:161-179
    ↓
人脸识别（是不是本人）
  SFace 提 128 维特征，与 identity/owner.npz 比余弦，阈值 0.45，
  连续 confirm_frames=2 帧才算数                                 config.yaml:89-107, identity.py:182
    ↓
分流
  本人 → identity.on_owner=skip（不抓拍/不录像/不播报/不推送）
  他人或认不出 → unknown_policy=alert（照常告警）                 config.yaml:109-117
    ↓
现场播报 + 远程通知
  语音：SAPI5 离线合成，require_face=true（要看清脸才出声），冷却 60s
  推送：notify.channel，支持 wxpusher / serverchan / wecom_bot / none   config.yaml:119-127, 183-207
    ↓
本地相册
  gallery.py，127.0.0.1:8765，jpg 当封面、点开播 mp4            gallery.py:326-330
    ↓
归档清理
  snapshot.keep_days=30（按天）+ storage.max_total_gb=20（按体积）  config.yaml:23, 158-168
```

### 模块职责（改代码前先看这里，别放错地方）

| 文件 | 职责 | 备注 |
|---|---|---|
| `watcher.py` | 主程序 + 命令行入口 | 采集/检测/抓拍/录像/语音/推送/清理 |
| `logic.py` | **纯逻辑层**：连续帧确认、挑帧、冷却、录像收尾策略、归档清理 | 不依赖摄像头和网络，是本项目单测覆盖率最高的部分，新增逻辑优先放这里 |
| `recorder.py` | 环形 JPEG 预录 + 后台线程写 H.264 | |
| `guardian.py` | 外部守护进程，watcher 死了把它拉起来 | |
| `gallery.py` | 本地相册 HTTP 服务 | 只用标准库，自实现 HTTP Range |
| `faces.py` / `identity.py` | YuNet 人脸检测 / SFace 人脸识别 | |
| `speaker.py` / `loudness.py` / `audio_device.py` | SAPI5 语音 / 响度处理 / 系统音量读写保底 | |
| `notifier.py` / `setup_push.py` | 推送渠道 / 推送配置引导 | |
| `audio_recorder.py` | 录音，收尾后 ffmpeg 并音轨 | |

---

## B. 常用命令

### B.0 先说解释器（最容易踩的一条）

**本项目能跑的解释器不是系统 Python**，而是：

```
C:\Users/user\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

- 版本 Python 3.13.14；已装 cv2 5.0.0 / numpy 2.5.4 / ultralytics 8.4.178 / pyttsx3 / sounddevice / psutil / pycaw / PyYAML。
- 在 git-bash 里写作 `/c/Users/user/.workbuddy/binaries/python/envs/default/Scripts/python`。
- 另有两个「装了 Python 但没有项目依赖」的解释器，直接 `python watcher.py` 会 `ModuleNotFoundError: No module named 'cv2'`：
  - `C:\Users/user\.workbuddy\binaries\python\versions\3.13.12\python.exe`（git-bash 里 `python` 默认解析到它）
  - `C:\Users/user\AppData\Local\Programs\Python\Python313\python.exe`（只有 numpy 和 yaml）
- 换机器或重装后重新确认的办法：`python -c "import cv2, numpy, yaml; print(cv2.__version__)"`，哪个能跑用哪个。

下面命令表里的 `python` 一律指上面那个能跑的解释器。

### B.1 watcher.py（出处：watcher.py:940-958）

| 目的 | 命令 |
|---|---|
| 看画面角度，拍一张 `preview.jpg` 就退出 | `python watcher.py --probe` |
| 试跑：只存档不推送，验证误报率 | `python watcher.py --dry-run` |
| 正式运行（前台常驻） | `python watcher.py` |
| 同时把日志写进文件 | `python watcher.py --log watcher.log` |
| 看监控是否还活着 | `python watcher.py --status` |
| 注册本人人脸（正对镜头自动采 12 张） | `python watcher.py --enroll` |
| 用一张已有照片注册 | `python watcher.py --enroll-from 某张正脸.jpg` |
| 辨认某张图里的人是不是本人 | `python watcher.py --whoami captures/2026-10-17/230913.jpg` |
| 试听处理后的语音（并打印增益报告） | `python watcher.py --say` |
| 试听未处理的原始语音 | `python watcher.py --say-raw` |
| A/B 对比：先原始、再处理 | `python watcher.py --ab` |
| 音量诊断：主音量 / 默认输出设备 / 各应用音量 | `python watcher.py --volume-check` |
| 直接把系统主音量设成 70% | `python watcher.py --volume-set 70` |
| 磁盘占用、容量上限、最旧/最新文件 | `python watcher.py --storage` |
| 用另一份配置启动 | `python watcher.py --config config.local.yaml` |

### B.2 guardian.py（出处：guardian.py:438-441）

| 目的 | 命令 |
|---|---|
| 常驻守护（每 `guardian.check_seconds` 秒查一次） | `python guardian.py` |
| 看守护状态、累计重启次数、最近动作 | `python guardian.py --status` |
| 只检查一次就退出（幂等，给计划任务用） | `python guardian.py --once` |
| 只报告该做什么，不真的重启 | `python guardian.py --dry-run` |
| 指定配置 | `python guardian.py --config config.yaml` |

### B.3 gallery.py（出处：gallery.py:326-330）

| 目的 | 命令 |
|---|---|
| 起本地相册（默认 127.0.0.1:8765） | `python gallery.py` |
| 换端口 / 换目录 / 换状态文件 / 换判死阈值 | `python gallery.py --port 8765 --captures captures --status-file status.json --stale-after 120` |

### B.4 setup_push.py（出处：setup_push.py:142-147）

| 目的 | 命令 |
|---|---|
| 交互式配置推送 | `python setup_push.py` |
| 非交互：直接写 wxpusher | `python setup_push.py --channel wxpusher --token AT_xxx --uid UID_xxx` |
| 非交互：直接写企业微信机器人 | `python setup_push.py --channel wecom_bot --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"` |
| 只写配置不发测试消息 | 追加 `--no-test` |

`--channel` 的合法取值只有：`wxpusher`、`serverchan`、`wecom_bot`（外加 config.yaml 里的 `none`）。

### B.5 启动脚本

- `start-room-watch.bat` —— 单独拉起 watcher。
- `start-with-guardian.bat` —— 守护 + 监控一起管起来。
- `install-autostart.ps1` —— 配置开机自启（需要你亲手执行）。

---

## C. 怎么跑测试

八个测试文件全部自带 `if __name__ == "__main__": unittest.main()`，**不需要 `-m unittest`**，直接跑文件即可。

| 文件 | 用例数 | 命令 | 需要什么 |
|---|---:|---|---|
| `test_logic.py` | 56 | `python test_logic.py` | 纯标准库，任何 python 都能跑 |
| `test_loudness.py` | 32 | `python test_loudness.py` | numpy，纯数学 |
| `test_recorder.py` | 19 | `python test_recorder.py` | cv2，**会真写视频**（约 3 秒，顺便打印 720p 预录 JPEG 编码实测 ms/帧） |
| `test_gallery.py` | 18 | `python test_gallery.py` | cv2，**真起 HTTP 服务**（约 17 秒），验证 Range 206 与目录穿越防护 |
| `test_guardian.py` | 31 | `python test_guardian.py` | yaml + psutil，不真重启进程 |
| `test_identity.py` | 31 | `python test_identity.py` | cv2 + 合成图像，不需要真实摄像头 |
| `test_audio_storage.py` | 24 | `python test_audio_storage.py` | 临时文件，不需要麦克风 |
| `test_integration.py` | 6 | `python test_integration.py` | **假摄像头 + 假 YOLO**，不需要真实摄像头，但耗时最长（约 56 秒） |

合计 **217 项**。2026-10-18 实测：八个文件全部 `OK`。

全量跑：

```bash
cd C:/Users/user/WorkBuddy/2026-10-16-14-53-33/room-watch
PY=/c/Users/user/.workbuddy/binaries/python/envs/default/Scripts/python
for f in test_logic.py test_loudness.py test_recorder.py test_gallery.py \
         test_guardian.py test_identity.py test_audio_storage.py test_integration.py; do
  echo "===== $f ====="; "$PY" "$f" 2>&1 | tail -4
done
```

八项测试都不联网、不发推送、不读真实摄像头。唯一会占用资源的是 `test_gallery.py` 起的 HTTP 服务
（若 8765 已被正式 gallery 占用，先停掉正式的，或换个端口）。

---

## D. 踩过的坑 / 硬约束

以下每一条都是本项目真实发生过的事故，按「现象 → 原因 → 正解」记录。

### D.1 摄像头同时只能被一个进程占用；改完代码必须重启

- **现象**：第二份 `watcher.py` 打不开摄像头；改完 `config.yaml` 后运行中的进程行为不变。
- **原因**：摄像头设备独占；配置只在 `Watcher.__init__` 里读一次（watcher.py:139-175），运行中不热加载。
- **正解**：改完代码或配置，**必须重启 watcher 和 gallery 两个进程**才生效。不要靠「再开一份」来验证。

### D.2 判断 watcher 是否活着，必须看 `status.json` 的 `last_frame_at` 距今

- **现象**：进程被强杀后，读 `status.json` 里的 `stale` 字段永远是 `false`，会把死掉的程序报成在线。
- **原因**：进程被杀前来不及再写一次状态，磁盘上留下的最后一份快照里 `stale`/`camera_ok` 都还是「正常」。
- **正解**：在**读取的那一刻**重算：`now - last_frame_at > heartbeat.stale_after_seconds`（默认 120 秒）即判定停摆。
  代码里就是这么做的，见 watcher.py:1045-1050（`print_status` 里有明确注释：绝不能信文件里存的 stale 字段）。
  结论：**不要自己解析 `stale` 字段，用 `python watcher.py --status`**。

### D.3 本机 `Start-Process`（PowerShell）无法启动进程

- **现象**：`Start-Process` 报「已添加项。字典中的关键字:'Path'所添加的关键字:'PATH'」，进程起不来。
- **原因**：环境里 `Path` 与 `PATH` 重复键，.NET 的进程启动环境字典抛异常。
- **正解**：启动后台进程**一律用 bash 后台方式**（`cmd //c start` 与 WMI 也都被本机安全规则拦过），绕开 `Start-Process`。

### D.4 录像必须用 avc1(H.264)；`Failed to load OpenH264 library` 可忽略

- **现象**：日志里刷 `Failed to load OpenH264 library`。
- **原因**：本机 OpenCV 5.0 只有 avc1(H.264) 和 mp4v 能写；浏览器基本只认 H.264。报错来自 FFmpeg 后端，
  实际由 Media Foundation 接手成功编码。
- **正解**：`record.codec` 保持 `avc1`；那条 stderr 报错是噪音，不用管。mp4v 只在兜底时用，网页放不了。
  出处：config.yaml:33-36、recorder.py:36-40。

### D.5 H.264 编码 11~18ms/帧，必须后台线程写盘

- **现象**：把编码放进主循环会让检测帧率掉下来。
- **原因**：10fps 主循环每帧预算 100ms，H.264 单帧 11~18ms 看似够，但叠加预录与磁盘抖动后会拖慢检测。
- **正解**：后台线程写盘；`record.queue_size`（默认 120）满时**宁可丢帧也不许拖慢检测主循环**。
  出处：config.yaml:55、recorder.py。

### D.6 录像有 8 秒预录，合并音轨要用 ffmpeg `adelay` 补等长静音

- **现象**：并好音轨后，声音整段晚 8 秒，对不上画面。
- **原因**：预录的 8 秒画面发生在「开始录音」之前，音轨长度天然比视频短。
- **正解**：合并时给音轨补等长静音：`-af adelay=<预录毫秒>:all=1`（视频流 copy，不重编码）。
  出处：config.yaml:62-64、audio_recorder.py:14 与 :50、test_audio_storage.py:35（断言 `adelay=8000:all=1`）。

### D.7 `alignCrop()` 要传**原图**和**原图坐标系**的关键点

- **现象**：人脸识别认错人，但不报任何错。
- **原因**：YuNet 是在 YOLO 人形框的裁剪图上跑的，它返回的 15 维行是**裁剪图坐标**。
  直接交给 `alignCrop()` 会把脸摆歪、特征全废 —— 而这个错误是**静默**的。
- **正解**：必须把关键点补回人形框偏移量再传下去。已有实现 `faces.py:145-158` 的 `_shift_row()`，
  调用点在 faces.py:124-126。任何新增的人脸相关调用都要走同一条路。
  出处：faces.py:38、:124-126、:176-178；identity.py:21、:182。

### D.8 编辑 `config.yaml` 时极易把相邻行合并、吞掉某个键

- **现象**：`behavior.idle_threshold_seconds` 曾经被吞进上一行的注释里，配置项静默失效。
- **原因**：增量编辑时覆写了相邻行。
- **正解**：对 `config.yaml` 一律**最小增量修改**，改完立刻复读确认该键还在、缩进没变。
  不要顺手重排或重写整个文件。

### D.9 `events_today` 是内存计数，重启归零

- **现象**：重启 watcher 后 `--status` 显示今日事件数为 0，但今天明明有事件。
- **原因**：`events_today` 只在进程内存里累加，不落盘。
- **正解**：真实事件数查 `events.db`（SQLite，`storage.database`）。判断「今天有没有人进来」不要只看 `events_today`。

### D.10 三个 Python 里只有一个能跑（本条为本次建库时新增确认）

- **现象**：`python watcher.py` 报 `ModuleNotFoundError: No module named 'cv2'`。
- **原因**：git-bash 的 PATH 把 `python` 解析到了没有项目依赖的解释器。
- **正解**：用 B.0 里那个 `envs/default` 解释器，或设置 `ROOMWATCH_PYTHON` 环境变量。
  验证命令：`python -c "import cv2, numpy, yaml; print(cv2.__version__)"`。

---

## E. Git 与隐私约定

### E.1 什么入库、什么不入库

完整清单见 `.gitignore`（每条都写了原因）。摘要：

- **入库**：全部 `*.py`（含 8 个测试）、`config.yaml`、`README.md`、`AGENTS.md`、`requirements.txt`、
  `models/*.onnx`（YuNet 230KB + SFace 37MB）、`yolov8n.pt`（6.5MB）、
  `start-*.bat`、`install-autostart.ps1`、`.githooks/`、`.gitignore`、`.gitattributes`。
  > `models/` 与 `yolov8n.pt` **必须入库**：这台机器下载该模型实测只有 20KB/s，37MB 要半小时，属于稀缺资产。
- **不入库**：`captures/`（约 1.9GB 抓拍与录像，强隐私）、`identity/owner.npz`（生物特征）、
  `voice_cache/`（可自动重建）、`events.db`、`status.json`、`guardian_state.json`、`guardian.lock`、
  `*.log`、`__pycache__/`、`preview.jpg`、`demo_recording.mp4`、`tts_test.mp3`、`.workbuddy/`。

### E.2 `config.yaml` 入库，但凭据必须是占位串

`config.yaml` 里有四个凭据键，入库时**必须**是占位串：

| 键 | 占位串 |
|---|---|
| `notify.wxpusher.app_token` | `AT_xxx_replace_me` |
| `notify.wxpusher.uid` | `UID_xxx_replace_me` |
| `notify.serverchan.sendkey` | `SCTxxx_replace_me` |
| `notify.wecom_bot.webhook` | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME` |

**凭据泄漏风险靠提交前钩子拦截，不靠忽略文件。**

### E.3 钩子

- 位置：`.githooks/check_secrets.py`（判定逻辑）+ `.githooks/pre-commit`（薄壳）。
- 生效方式：`git config core.hooksPath .githooks`。
  **不要手工改 `.git/hooks/`**（那个目录不在版本控制里，换机器就没了）。
- `core.hooksPath` 是**本地配置，不随仓库分发**：每次新克隆都要重新执行一次上面的命令。
- 单独运行 / 自测：
  ```bash
  python .githooks/check_secrets.py --self-test          # 内置正例+负例自测，含端到端笺证
  python .githooks/check_secrets.py --no-git             # 只查 config.yaml
  python .githooks/check_secrets.py --config 某文件.yaml --no-git
  ```
- 两条检查线：① `config.yaml` 里四个凭据是否填了真值；② 暂存区里是否混进了
  `captures/**`、`identity/**`、`voice_cache/**`、`.workbuddy/**`、`*.db`、`*.log`、`*.npz`。
- 判定是**确定性**的（文本前缀 + 正则 + 路径通配），不做「看起来像密钥」的推断。
- 命中时会打印**具体行号、命中的值、风险说明、以及三种修法**，照做即可。

### E.4 想在本机长期保留真实凭据，三条路（后果已写明）

1. **改回占位串再提交**（推荐，也是钩子放行的唯一方式）。
2. **复制一份 `config.local.yaml` 填真值**（已被 `.gitignore` 忽略），
   启动时用 `--config config.local.yaml` 指定 —— 这个 flag 在 watcher.py:940 与 guardian.py:438 都真实存在。
   gallery.py 没有 `--config`，它只读 `status.json`。
3. **`git update-index --skip-worktree config.yaml`**：
   只对你这台机器生效；**执行后你对 `config.yaml` 的所有修改 git 完全看不见**（`git status` 不显示、也提交不了）。
   要恢复必须执行 `git update-index --no-skip-worktree config.yaml`。
   用了它就必须自己记住，否则将来会困惑于「我改了配置怎么提交不上去」。

### E.5 换行与中文路径

- `.gitattributes` 已规定：`*.bat`/`*.ps1`/`*.cmd` → CRLF；`*.py`/`*.md`/`*.yaml` 等 → LF；
  图片/音视频/`*.npz`/`*.pt`/`*.onnx`/`*.db` → 二进制（禁止任何换行转换）。
- 本机 git 全局 `core.autocrlf=true`，靠 `.gitattributes` 覆盖它。
- 仓库本地已设 `core.quotepath=false`，`git status` 不会把中文路径转义成 `\344\275\240` 这种形式。
- `.githooks/pre-commit` 被单独规定为 LF：CRLF 的 shebang 会让 Git for Windows 的 sh 找不到解释器，钩子会静默失效。

---

## F. 给 AI 的行为约定

以下是对未来 AI 助手的硬性要求，用户明确要求写在这里。

1. **动手前先读两份东西**：本文件 `AGENTS.md`，以及 `.workbuddy/memory/` 下最近的工作日志
   （逐小时活动流水，里面有进程 PID、抓拍内容判断、当天已做过什么）。
2. **未经批准不得杀死正在运行的 watcher / gallery 进程**。
   它们可能正在录像，硬杀会损坏当前 mp4 段。确需重启，先确认 `status.json` 里 `recording` 为 `false`
   （读法：`python watcher.py --status`，或直接看 status.json）。
3. **不要同时启动两份实例**。摄像头独占，第二份起不来，还会让第一份的判活逻辑失真。
4. **修改 `config.yaml` 用最小增量，改完复读确认**。不要顺手重排、重写整个文件（见 D.8）。
5. **代码风格跟随现有代码**：中文注释、类型注解、纯函数优先便于单测。
   `logic.py` 是纯逻辑层的范本：不依赖摄像头和网络，新逻辑优先放这里而不是塞进 `watcher.py`。
6. **任何新功能都要配可离线运行的单测**，并且要能在本机跑通（参考 C 节的八个测试文件）。
7. **不要编造命令**。本文件没写的命令，先 `--help` 或读源码确认存在再用。
8. **提交前不要绕过钩子**。若钩子阻断，按它打印的修改方法处理，不要用 `--no-verify`。
9. **不要 `git add -f` 隐私文件**，也不要删除磁盘上的 `captures/`（约 1.9GB，是用户的数据）。

---

## G. 当前已知待办（含为何暂缓）

按「要么现在做完、要么写明为何暂缓」的要求列出。本次任务范围是建 Git 基建与写约定文档，
因此下面这些**不在本次范围**，逐条写明原因：

1. **gallery 的长期守护方案未定** —— 后台任务方式会被系统回收（2026-10-17 23:22 前后实测发生过，
   进程死亡、8765 无监听）。`guardian.py` 目前只守护 watcher，不管 gallery。
   候选方案：Windows 计划任务 / 改 `start-room-watch.bat` 同时拉起两个服务 / 开机自启。
   **暂缓原因**：需要用户在机器上确认安全策略（WMI 与 `Start-Process` 都被本机拦截，见 D.3），不是代码问题。
2. **录像在用户本人坐在电脑前时仍会录** —— `behavior.skip_when_user_active` 只抑制推送，不抑制录像。
   实测 14 分钟产出 4 段 mp4 共 653MB（约 46MB/分钟），长期会吃满磁盘。
   **暂缓原因**：这是产品决策（是否接受把自己也录下来），需用户拍板；改完还需重启 watcher。
3. **`README.md:473` 仍写着「test_gallery.py 16 项」，实际是 18 项**（2026-10-17 23:15 修裂图问题时加了两个用例）。
   **暂缓原因**：本次任务明确约定 `README.md` 只读，如需修正请交由用户确认后单独改。
4. **`start-room-watch.bat` / `start-with-guardian.bat` / `install-autostart.ps1` 当前在磁盘上是 LF**，
   `.gitattributes` 已规定它们应为 CRLF，但需要一次重新检出（或 `git add --renormalize` + 检出）才会在工作区生效。
   **暂缓原因**：本次只做首次提交，不改动现有文件内容；首次提交时它们以 LF 进入版本库，
   签出时会按 `.gitattributes` 转成 CRLF。首次提交后建议执行一次
   `git rm --cached -r . && git add .`（只动索引，不动磁盘文件）让索引与属性一致 —— 该操作尚未执行，待用户确认。
5. **`.gitignore` 里的 `config.local.yaml` 是一个约定而非现存文件** —— 目前磁盘上没有它。
   它是为 E.4 第 2 条方案预留的位置，只有当你真的要用「本机真凭据」方案时才创建。
