# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)：`主版本.次版本.修订号`。

- **版本号只有一个来源**：仓库根的 `VERSION` 文件。
  `roomwatch.__version__` 从它读取，`pyproject.toml` 从 `__version__` 读取，
  `rw version` 再打印它。三者不一致会被 `tests/test_paths.py` 拦下。
- **发版动作**：改 `VERSION` → 在本文件顶部加一节 → `git tag vX.Y.Z`。

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
