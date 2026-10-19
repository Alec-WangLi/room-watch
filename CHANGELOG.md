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

## [0.1.13] - 2026-10-19

> 稳定性收口：修掉真机上仍在的持续闪屏、相册端口冲突，以及几处状态写入的容错。

### 修复

- **界面持续闪屏**：打包后的界面没有 psutil，`procutil.pid_alive` 每 2 秒回退到
  `tasklist`，每次都弹一个控制台窗口。现在 psutil 随界面打包，构建自检会拦截缺失；
  同时 `tasklist` / `taskkill` 兜底调用统一加 `CREATE_NO_WINDOW`，
  即使将来 psutil 缺失也不会闪屏；界面里切换自启时调用 `schtasks` 同样隐藏控制台。
- **相册因系统保留端口起不来**：端口被 Windows 保留（如本机 8748–8847，
  bind 报 WinError 10013）或不可用时，相册自动改用备用端口（优先固定偏移，
  最后才用系统分配的端口），并把实际地址写入 `var/state/gallery.json`；
  界面读取实际地址打开浏览器。
- **guardian 对相册崩溃循环失效**：以前只要 Popen 成功就清零失败计数，
  相册启动后立刻退出时永远进不了退避、也不会告警。现在只有下一次检查
  仍存活才算恢复；连续失败会按退避重试并告警一次。
- **状态写入容错**：健康快照、状态文件、守护状态、崩溃退出记录的写入失败
  不再中断主循环或启动；原子写使用唯一临时文件名，并重试 Windows 上
  目标文件被瞬时占用的情况。

### 构建

- 界面打包环境安装 psutil 并加 `--hidden-import psutil`；
  `room-watch.exe --selftest` 在打包产物里检查 psutil 是否存在。
- 安装器在复制文件前先用旧版 `rw stop` 优雅停止监控，再按安装目录路径
  清理 guardian / gallery / 界面进程；升级不再卡在「无法自动关闭所有应用程序」。

---

## [0.1.12] - 2026-10-19

> M1：guardian 成为唯一启动者，并同时守护监控与相册；用户主动停止后不会被拉起。

### 变更

- **`rw start` 改为后台启动**：写 `desired_state=running`，拉起常驻 guardian，
  并立即触发一次 `guard --once`。旧的现场调试行为保留为 `rw start --foreground`。
- **`rw stop` 同时停止相册**：先写 `desired_state=paused`，再停 gallery 与 watcher；
  guardian 看到 paused 后不再重启任何服务。
- **暂停语义**：GUI「停止监控」同样先写 paused；guardian 不会发送「已恢复」通知。

### 新增

- **guardian 守护 gallery**：相册写 `var/state/gallery.pid`；guardian 用
  PID + 命令行校验判活，PID 复用不会误杀，挂了自动拉起并写重启历史。
- **guardian 单实例**：`var/state/guardian.owner.lock` 由常驻 guardian 持有，
  重复启动的实例直接退出；`--once` 仍用原来的短锁。
- **恢复/崩溃通知**：watcher 重启后，下一次健康检查通过时发一条
  「已恢复，上次退出原因」；窗口内重启次数达到 `guardian.crash_threshold`
  （默认 10 分钟 3 次）时发「反复崩溃」告警。通知走后台线程，不阻塞守护。
- **启动自检**：模型文件、运行目录可写、推送凭据、磁盘余量四项；失败项写入
  `health.json`，guardian 会通知一次，GUI 状态栏显示。
- **自启抽象**：优先注册计划任务（登录触发、失败自动重启、零闪窗）；
  被安全策略拦截时回退 HKCU Run + VBS，并清理另一条旧入口，避免双实例。

### 修复

- guardian 拉起 watcher 时改用 `--foreground`，避免再次进入 `rw start` 的后台分支；
- 全新安装（没有 `status.json`）时首次启动 watcher，不再永久等待；
- guardian 通知改为读取 `config.local.yaml`，恢复/崩溃通知才能真正发出；
- 退出记录校验 PID，避免把上一次正常退出误当成本次原因；
- 崩溃循环按窗口内重启次数退避；自检不再把缺失的 `.pt` 当失败（ONNX 存在时）。
- GUI 检查更新、找解释器、拍预览/注册等子进程统一加 `CREATE_NO_WINDOW`，不再闪 Python 控制台窗口。

### 文档

- README 说明电脑需保持登录、自启可能走计划任务或 Run 键；
- AGENTS 增加暂停语义与新增状态文件；architecture 更新 guardian 职责。

---

## [0.1.11] - 2026-10-19

> M0：先把「为什么退出」变成可回答的问题。不改变监控行为，只增加观测能力。

### 新增

- **健康快照** `var/state/health.json`：每秒写一次 pid、循环序号、最后画面/推理时间、
  连续错误数、摄像头重开次数、RSS/CPU、自检结果。guardian、界面与诊断包共用。
- **退出记录** `var/state/last_exit.json`：正常停止、摄像头打不开、模型加载失败、
  未处理异常、主循环停滞都会留下退出码与原因；guardian 重启时读取并写入历史。
- **崩溃钩子**：`faulthandler` 写入 `var/logs/watcher-fault.log`（原生崩溃，
  含全部线程栈）；`sys.excepthook` / `threading.excepthook` 记录 Python 未捕获异常。
- **停滞看门狗**：主循环序号超过 `health.stall_seconds`（默认 120 秒）没变化时，
  先转储线程栈，再以 `EXIT_STALLED` 退出，交给 guardian 重启。
- **守护重启历史** `var/state/guardian_history.jsonl`：每次重启记录动作、退出码与原因；
  `rw guard --status` 显示上次退出原因与最近 5 条历史。
- **日志轮转**：watcher 与 guardian 的日志改为 5MB × 5 份轮转。
- **`rw diag`**：生成脱敏诊断包（日志尾部、健康快照、退出记录、守护历史、
  脱敏后的 `config.local.yaml`、doctor 输出、系统信息），不收录 `data/` 下的图像。

### 退出码

`0` 正常 / `2` 配置错误 / `3` 摄像头不可用 / `4` 模型加载失败 /
`5` 未处理异常 / `6` 主循环停滞。

### 测试

新增 `test_health.py`、`test_diagnostics.py`、`test_diag.py`；
集成测试增加健康快照与退出记录的断言。

---

## [0.1.10] - 2026-10-19

> 真机升级 0.1.9 失败后补的修复：安装器按 `rc=5` 回滚，版本号原地不动，
> 随后自动重开界面弹出「Failed to load Python DLL」。

### 修复

- **相册服务开着时更新必然失败**：`rw update` 原来只停 watcher，而相册
  （`python -m roomwatch gallery`）也跑在包内 `runtime\python` 上，占着
  `python313.dll`。Inno 装前的 RestartManager 关不掉一个没有消息循环的控制台
  进程，30 秒后按 Abort → 安装器返回 5 并整段回滚（实测 04:00:49 的日志）。
  新增 `updater.stop_all()`：按安装目录挑出所有 roomwatch 进程，
  watcher 走优雅停止（等录像收尾），相册/守护直接终止；`rw update` 改用它。
- **安装失败后重开界面撞上解包被打断**：回滚还没收完尾脚本就 `start` 界面，
  PyInstaller 的解包子进程被打断，弹原生错误框
  `Failed to load Python DLL ... _MEIxxxx\python313.dll`。现在失败分支先等 2 秒。

### 测试

`test_updater.py::StopAllTest` 新增 6 项：按安装目录选进程、忽略同目录的
ffmpeg、路径大小写不敏感、字段缺失不崩、停不掉要如实报告。

---

## [0.1.9] - 2026-10-19

> 三个实测反馈的修复：停止后按不了启动、系统音量被改、界面没有配置入口。
> 设计说明见 `docs/gui-0.1.9-design.md`。

### 修复

- **停止监控后「启动监控」按不动（最长 120 秒）**：界面原来只按
  `status.json` 的心跳时间判活，而 `taskkill /F` 之后那条心跳仍然很新。
  现在判活是「心跳新鲜 **且** PID 还活着」，两条都成立才算在线；
  实测强杀后 1 秒内就显示「已停止」、启动按钮可用（0.1.7 要等 120 秒）。
- **停止改为两段式**：先写 `var/state/stop.request` 让监控自己退出循环，
  走既有的 `finally` 把 mp4 收尾、音轨合并、音量还原，最多等 20 秒；
  超时才在用户确认后 `taskkill /F`。实测优雅停止用时 1 秒，进程干净退出。
  界面在停止过程中显示「正在停止…（录像收尾中）」，两个按钮同时锁住。
- **系统音量被改成 70%**：录音走的是麦克风输入，与系统播放音量无关；
  抬主音量只是为了 TTS 播报听得见，代价却是改掉用户的音量设置，
  而且强杀时还原不回来（日志实测 `主音量 2.0% → 70.0%`）。
  新增 `voice.volume_mode`：`off`（默认，完全不碰）/ `session`
  （只提本程序音轨）/ `master`（旧行为，显式选择才生效）。
  `config.yaml` 默认改成 `off`，升级即生效；`master` 档另加崩溃兜底备份，
  下次启动自动补还原。
- **拍预览图看不到结果**：`probe` 写的是 `var/media/preview.jpg`，
  界面只扫 `data/captures/`，两者不是同一处。现在两处都看、按修改时间取新的，
  标题写明来源与时间；`probe --json` 输出尺寸/耗时/失败原因，界面直接弹结论。
- **注册本人不知道生不生效**：原先只弹一个黑框控制台，界面拿不到结果。
  现在捕获输出、解析退出码，完成后弹结果；主窗口常驻显示
  「本人模板：已注册 · N 个样本 · 时间」，未注册时明确提示所有脸按访客处理。

### 新增：界面「设置」（四个页签）

- **推送**：渠道、企业微信 webhook、WxPusher、Server酱、录像推送开关，含「发送测试消息」。
- **身份**：模板状态（样本数/时间/维度）、注册本人、用最新抓拍注册、清除模板，
  以及阈值与两种策略（认不出的人 / 认出本人）。
- **自启**：`HKCU\...\Run\RoomWatch` 的开/关与状态回读；指向别的安装目录时会警示。
  改用 `winreg` 而不是 `reg.exe`（后者在本机策略与沙箱里常被拦）。
- **声音**：播报开关/文案/冷却，以及上面的三档音量策略。

所有设置写 `config.local.yaml`，**不写 `config.yaml`** ——
安装包升级会整体覆盖主配置，写那里的设置下次更新就丢了。
凭据只回「已设置 …末4位」，全文不出进程；保存后重新读一遍生效值，
读回不一致直接报错，不静默成功。

### 新增命令

`rw stop`（优雅停止）、`rw config`（`--json` / `--set 键=值` / `--test`）、
`rw autostart`（`--json` / `--enable` / `--disable`）、`rw identity`（`--json` / `--clear`）。

### 测试

新增 `test_procutil.py`(8) / `test_control.py`(10) / `test_settings.py`(16) /
`test_autostart.py`(12) / `test_volume_mode.py`(15)，并扩充 `test_gui_state.py`
（PID 判活、stopping 相位、预览图优先级、按钮可用性矩阵）。
界面自检 `--selftest` 现在会连设置窗口一起建出来，校验四个页签。

---

## [0.1.8] - 2026-10-19

### 修复

- **相册服务被「客户端断开」刷屏**：手动跑 `rw gallery` 挂 2 小时 24 分钟，
  日志里攒了 23 段
  `Exception occurred during processing of request`，每段 30 行堆栈，末行一律是
  `ConnectionAbortedError: [WinError 10053]`。关页面、拖视频进度条都会触发。
  根因是 `serve_media()` 只捕 `BrokenPipeError`/`ConnectionResetError`，
  而 Windows 上「浏览器正常关闭、服务端还在写」抛的是第三个类 `ConnectionAbortedError`。
  现在统一到 `gallery.CLIENT_GONE` 四元组，并新增 `GalleryServer.handle_error()`
  兜住写响应头/首页/401 阶段的同类断开；真异常（如 `ValueError`）照旧打印。
- **媒体文件打不开时不再留半截响应**：文件在列目录后被删除/被占用，原来异常直接
  抛出连接，浏览器侧表现为卡住；现在显式回 500。

### 测试

- `tests/test_gallery.py::TestClientDisconnect` 新增 5 项，用真 socket 覆盖两种断法
  （`SO_LINGER` 造 RST→10054、普通 `close()`→10053），断言 stderr 无堆栈且服务
  随后仍能回 200；另有 2 项钉住 `handle_error` 只吞连接类异常。全库 462 项通过。

---

## [0.1.7] - 2026-10-19

### 修复

- **更新一次就泄漏 142MB**：下载好的 `setup.exe` 一直留在 `var/cache/updates`，
  装完也不删。实测连续更新两次后 `var/` 涨到 285MB（安装目录本身才 368MB）。
  现在下载前先 `updater.prune_downloads()` 清掉旧包，交接时再删掉缓存里那份
  （helper 用的是 `%TEMP%` 里的暂存副本），只有失败的包会留下来便于排查。

---

## [0.1.6] - 2026-10-19

> **升级须知（一次性）**：0.1.5 及更早版本的 `rw update` 就是坏的（见下），
> 停在那些版本上的机器**没法自己升上来**，需要手工下载 `room-watch-0.1.6-setup.exe`
> 装一次；从 0.1.6 起更新链路才可用。

### 修复

- **`rw update` 在真实安装版上必然失败（返回 5 并回滚）**：更新器是用包内
  `runtime\python\python.exe` 跑的，Inno 的 RestartManager 发现「Python 占着
  要替换的 dll」，又关不掉自己的父进程，30 秒后静默模式按 Abort 处理。
  现象是「下载完、校验通过、然后报安装器返回 5」，且回滚会把安装目录弄脏。
  0.1.4 → 0.1.5 实测 100% 复现，日志：`Defaulting to Abort for suppressed
  message box (Abort/Retry/Ignore)`。
- 改为**交接安装**（`updater.handoff_install`）：先把安装包复制到 `%TEMP%`，
  再由一个与安装目录无关的 `cmd.exe` 脚本等更新器退出后执行安装器；
  装完按场景把界面或监控拉起来，失败则至少把界面打开，日志写
  `var/logs/update.log`。命令行会立即返回「已交接」而不是假装装完了。
- **中文 Windows 下的假死**：`tasklist` 输出是 GBK，按 UTF-8 解码会在
  subprocess 读线程里抛 `UnicodeDecodeError`；改成按字节匹配 PID。
- 交接脚本**不写死路径**，改用命令行参数传入 —— 客户机用户名带中文时
  （`C:\Users\张三\AppData\...`）脚本仍能保持纯 ASCII，不会乱码。

---

## [0.1.5] - 2026-10-19

### 新增

- **界面里直接更新**：主界面右上角新增「检查更新」按钮，有新版时按钮文字变成
  「有新版本 0.1.x」，点一下弹确认（附更新说明节选）。确认后界面自动关闭，
  更新在独立控制台窗口里跑（下载百分比可见），装完自动重新打开界面。
  版本纪律仍由 `updater.decide` 一处判定，界面只做展示与确认，不自己比对版本。
- **启动后台自检**：按 `update.check_interval_hours`（默认 24 小时）在界面启动时
  后台查一次，结果缓存在 `var/state/update.json`，下次启动直接复用，不重复联网；
  设为 0 关闭后台自检。查到的更新**只改按钮文字，不弹窗、不自动安装**。
- `rw update --json`：输出一行 JSON（`current` / `latest` / `policy` / `tag` /
  `notes` / `error` / `check_interval_hours`），隐含 `--check`，供界面消费。
- `rw update --wait-exit PID` 与 `--relaunch-gui`：先等指定进程退出再安装、
  装完自动重新打开界面。

### 修复

- **界面开着时更新会静默失败**：界面进程占着 `room-watch.exe`，安装器替换不掉
  正在运行的可执行文件，现象是「提示更新成功，但版本号没变」。现在界面把自身
  PID 交给更新子进程（`--wait-exit`）并先退出，装完再经 `--relaunch-gui` 重开。
- 客户此前**没有任何发现更新的途径**（GUI 无入口、后台自检未实现），只能靠
  别人告知进安装目录跑 `rw.cmd update`；本次一并补上。

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
