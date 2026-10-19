# M0–M1 可观测性与守护自愈 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 room-watch 的每次退出都有原因可查，并让 guardian 成为唯一启动者，同时守护 watcher 与 gallery，支持用户主动暂停而不被自动拉起。

**Architecture:** M0 只增加观测能力：健康快照、崩溃钩子、退出码、退出记录、日志轮转、诊断包、重启历史。M1 在此之上改造守护：期望状态文件（running/paused）、gallery PID 监督、一次性启动锁、崩溃阈值通知、恢复通知、自检，以及优先计划任务、失败回退 Run 键的自启抽象。

**Tech Stack:** Python 3.10+（实测 3.13）、标准库 logging/faulthandler/zipfile/winreg、psutil（已有）、Tkinter（已有）、unittest、Windows `schtasks.exe`、Inno Setup。

**Spec:** `docs/superpowers/specs/2026-10-19-reliability-productization-design.md`

## Global Constraints

- 目标平台 Windows 10/11 x64；per-user 安装，不要求管理员权限。
- USB 摄像头只能在用户登录会话中采集；**不得**把采集做成 Windows 服务。
- 摄像头设备独占，任何时刻只允许一个 watcher。
- 版本纪律：`VERSION` 只允许动第三位（修订号）；本计划 M0、M1 各发一个修订版。
- 许可证 AGPL-3.0；不新增强制第三方依赖。
- 所有测试离线可跑，不依赖真实摄像头与网络；测试文件必须带 `unittest.main()`。
- 现有配置、`config.local.yaml` 深合并、`data/` 隐私、`var/` 可重建的约定不变。
- 提交前不得绕过 `.githooks`，不得使用 `--no-verify`。
- 提交信息用中文，类型前缀沿用现有风格；提交信息末尾加：
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Review Focus

- **期望状态文件缺失或损坏**：默认必须是 `running`，损坏时也按 `running` 处理；否则用户会以为监控在跑，实际 guardian 永久待机。测试放在 Task 7。
- **计划任务被安全策略拦截**：必须回退到 HKCU Run + VBS；两者都失败时 `enable()` 必须返回失败与原因，绝不报告成功。测试放在 Task 9。
- **退出记录缺失或损坏**：guardian 仍必须重启 watcher，原因记为 `unknown`，不得因读不到记录而卡住。测试放在 Task 4。
- **用户主动暂停**：guardian 不得重启 watcher/gallery，且不得发送「已恢复」通知；残留的 `pending_recovery` 必须清除。测试放在 Task 11。
- **gallery PID 陈旧或 PID 复用**：必须校验命令行包含 `roomwatch` 与 `gallery`，否则按已死处理。测试放在 Task 7。

---

## Task 1: 健康快照、退出码与退出记录

**Files:**
- Create: `roomwatch/health.py`
- Create: `tests/test_health.py`
- Modify: `roomwatch/paths.py`（新增 `DIAG_DIR`，本任务先加，后续 Task 5 使用）

**Interfaces:**
- Consumes: 无。
- Produces:
  - `EXIT_OK=0`、`EXIT_CONFIG=2`、`EXIT_CAMERA=3`、`EXIT_MODEL=4`、`EXIT_UNHANDLED=5`、`EXIT_STALLED=6`
  - `write_json_atomic(path: str | Path, payload: dict) -> None`
  - `read_json(path: str | Path) -> Optional[dict]`
  - `build_health_payload(*, pid: int, loop_seq: int, started_at: str | None, last_frame_at: str | None, last_inference_at: str | None, consecutive_errors: int, camera_reopens: int, selfcheck: list[dict] | None = None, extra: dict | None = None) -> dict`
  - `proc_stats() -> tuple[Optional[float], Optional[float]]`（返回 `(rss_mb, cpu_pct)`）
  - `write_health(path: str | Path, payload: dict) -> None`
  - `write_exit_record(path: str | Path, *, pid: int, code: int, reason: str, at: str | None = None) -> None`
  - `read_exit_record(path: str | Path) -> Optional[dict]`
  - `stall_exceeded(loop_seq: int, last_seq: int, unchanged_seconds: float, threshold: float) -> bool`
  - `watchdog_loop(*, get_seq, dump_stacks, on_stall, sleep, stop_event, clock=time.monotonic, poll_seconds=5.0, threshold_seconds=120.0) -> None`

- [ ] **Step 1: 写失败测试**

`tests/test_health.py` 至少包含：

```python
class AtomicJsonTest(unittest.TestCase):
    def test_write_leaves_no_tmp_and_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state" / "health.json"
            health.write_json_atomic(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})
            self.assertFalse((p.parent / "health.json.tmp").exists())

    def test_read_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{oops", encoding="utf-8")
            self.assertIsNone(health.read_json(p))

class ExitRecordTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            health.write_exit_record(p, pid=123, code=health.EXIT_UNHANDLED, reason="unhandled")
            rec = health.read_exit_record(p)
            self.assertEqual((rec["pid"], rec["code"], rec["reason"]), (123, 5, "unhandled"))
            self.assertTrue(rec["at"])

class StallTest(unittest.TestCase):
    def test_strictly_greater_than_threshold(self):
        self.assertFalse(health.stall_exceeded(1, 1, 120.0, 120.0))
        self.assertTrue(health.stall_exceeded(1, 1, 120.1, 120.0))

    def test_progress_resets(self):
        self.assertFalse(health.stall_exceeded(2, 1, 9999.0, 120.0))

class WatchdogLoopTest(unittest.TestCase):
    def test_stall_dumps_then_stops(self):
        seq = {"v": 1}
        clock = {"t": 0.0}
        calls = []
        stop = threading.Event()
        def sleep(s): clock["t"] += s
        def on_stall(): calls.append("stall"); stop.set()
        health.watchdog_loop(
            get_seq=lambda: seq["v"], dump_stacks=lambda: calls.append("dump"),
            on_stall=on_stall, sleep=sleep, stop_event=stop,
            clock=lambda: clock["t"], poll_seconds=5.0, threshold_seconds=10.0,
        )
        self.assertEqual(calls, ["dump", "stall"])
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_health.py`
Expected: `ModuleNotFoundError: No module named 'roomwatch.health'`

- [ ] **Step 3: 实现 `roomwatch/health.py` 与 `paths.DIAG_DIR`**

实现要点：
- `write_json_atomic` 写同目录 `.tmp` 后 `os.replace`，父目录 `mkdir(parents=True, exist_ok=True)`。
- `build_health_payload` 返回固定键：`pid`、`loop_seq`、`started_at`、`last_frame_at`、`last_inference_at`、`consecutive_errors`、`camera_reopens`、`rss_mb`、`cpu_pct`、`selfcheck`、`updated_at`；`extra` 逐键合并。
- `proc_stats` 用 `psutil.Process(os.getpid())`；任何异常返回 `(None, None)`，绝不让健康采集影响主循环。
- `watchdog_loop`：每次 sleep 后比较 `get_seq()`；seq 变化则重置计时；`unchanged_seconds > threshold_seconds` 时先 `dump_stacks()` 再 `on_stall()`，然后返回。
- `paths.py` 增加 `DIAG_DIR = VAR_DIR / "diag"`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_health.py`
Expected: `OK`（全部通过）

- [ ] **Step 5: 提交**

```bash
git add roomwatch/health.py roomwatch/paths.py tests/test_health.py
git commit -m "feat(health): 健康快照、退出码与停滞判定

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: 崩溃钩子与日志轮转

**Files:**
- Create: `roomwatch/logutil.py`
- Create: `roomwatch/diagnostics.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `health.EXIT_UNHANDLED`、`health.write_exit_record`。
- Produces:
  - `logutil.rotating_file_handler(path: str | Path, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> logging.Handler`
  - `logutil.event(logger: logging.Logger, name: str, **fields) -> None`（输出 `event=<name> key=value` 固定字段）
  - `diagnostics.Diagnostics(fault_log: str | Path, exit_record: str | Path)`
    - `.install() -> None`
    - `.note_exit(code: int, reason: str) -> None`
    - `.uninstall() -> None`
    - `.fault_file`（已打开的二进制文件句柄，供 `faulthandler.dump_traceback` 使用）

- [ ] **Step 1: 写失败测试**

```python
class RotatingHandlerTest(unittest.TestCase):
    def test_rotation_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.log"
            h = logutil.rotating_file_handler(p, max_bytes=200, backups=2)
            lg = logging.getLogger("rot-test"); lg.setLevel(logging.INFO)
            lg.addHandler(h); lg.propagate = False
            for i in range(20):
                lg.info("line %d %s", i, "x" * 40)
            h.close(); lg.removeHandler(h)
            self.assertTrue(p.exists())
            self.assertTrue((Path(tmp) / "x.log.1").exists())

class EventFormatTest(unittest.TestCase):
    def test_event_has_stable_key_value_format(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        lg = logging.getLogger("evt-test"); lg.setLevel(logging.INFO)
        lg.addHandler(h); lg.propagate = False
        logutil.event(lg, "camera_reopen", attempt=2)
        h.close(); lg.removeHandler(h)
        self.assertIn("event=camera_reopen", buf.getvalue())
        self.assertIn("attempt=2", buf.getvalue())

class DiagnosticsTest(unittest.TestCase):
    def test_note_exit_writes_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.note_exit(health.EXIT_UNHANDLED, "unhandled")
            rec = health.read_exit_record(Path(tmp) / "last_exit.json")
            self.assertEqual((rec["code"], rec["reason"]), (5, "unhandled"))

    def test_install_and_uninstall_restore_hooks(self):
        import sys, threading
        old_sys, old_thread = sys.excepthook, threading.excepthook
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.install()
            self.assertIsNot(sys.excepthook, old_sys)
            d.uninstall()
            self.assertIs(sys.excepthook, old_sys)
            self.assertIs(threading.excepthook, old_thread)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_diagnostics.py`
Expected: `ModuleNotFoundError: No module named 'roomwatch.logutil'`

- [ ] **Step 3: 实现**

- `rotating_file_handler` 用 `logging.handlers.RotatingFileHandler(..., encoding="utf-8")`。
- `event` 用 `logger.info("event=%s %s", name, " ".join(f"{k}={v}" for k, v in sorted(fields.items())))`。
- `Diagnostics.install()`：
  - 打开 `fault_log`（`"ab"`，无缓冲），`faulthandler.enable(file=self.fault_file, all_threads=True)`；
  - 保存并替换 `sys.excepthook`：先 `log.exception` 等价地写 traceback 到标准 logging，再 `note_exit(EXIT_UNHANDLED, "unhandled")`，最后调用原 hook；
  - 保存并替换 `threading.excepthook`：记录线程名与异常；
  - 注册 `atexit`：若尚未 note，写 `(EXIT_OK, "normal")`。
- `note_exit` 幂等：同一进程只写最后一次；`uninstall` 恢复 hook 并关闭文件。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_diagnostics.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/logutil.py roomwatch/diagnostics.py tests/test_diagnostics.py
git commit -m "feat(diagnostics): 崩溃钩子与日志轮转

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: watcher 接入健康快照、退出记录与停滞看门狗

**Files:**
- Modify: `roomwatch/watcher.py:144-284`（字段）、`:430-461`（状态写入）、`:700-900`（主循环）、`:1120-1212`（入口）
- Modify: `roomwatch/paths.py`（如需默认路径）
- Test: `tests/test_health.py`、`tests/test_integration.py`

**Interfaces:**
- Consumes: `health.build_health_payload`、`health.write_health`、`health.write_exit_record`、`health.watchdog_loop`、`diagnostics.Diagnostics`、`logutil.rotating_file_handler`。
- Produces:
  - `Watcher.loop_seq: int`
  - `Watcher.last_inference_at: datetime | None`
  - `Watcher.consecutive_errors: int`
  - `Watcher.camera_reopens: int`
  - `Watcher.selfcheck: list[dict]`
  - `Watcher.exit_reason: str`
  - `Watcher.health_path: str`
  - `Watcher.exit_record_path: str`
  - `Watcher.write_health(self) -> None`
  - 退出码：摄像头打不开 `EXIT_CAMERA`，模型加载失败 `EXIT_MODEL`，配置错误 `EXIT_CONFIG`，未处理异常 `EXIT_UNHANDLED`，停滞 `EXIT_STALLED`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_health.py` 增加：

```python
class WatcherHealthTest(unittest.TestCase):
    def test_write_health_contains_runtime_fields(self):
        from roomwatch.watcher import Watcher
        w = object.__new__(Watcher)
        w.health_path = str(Path(tempfile.mkdtemp()) / "health.json")
        w.loop_seq = 7; w.started_at = datetime.now()
        w.last_frame_at = datetime.now(); w.last_inference_at = datetime.now()
        w.consecutive_errors = 2; w.camera_reopens = 3
        w.selfcheck = [{"name": "模型", "ok": True, "detail": "ok"}]
        w.exit_reason = "normal"
        w.write_health()
        data = json.loads(Path(w.health_path).read_text(encoding="utf-8"))
        self.assertEqual(data["loop_seq"], 7)
        self.assertEqual(data["camera_reopens"], 3)
        self.assertEqual(data["selfcheck"][0]["name"], "模型")
        self.assertIn("rss_mb", data)
```

在 `tests/test_integration.py` 的 `_config()` 里增加：
`"health": {"path": os.path.join(self.tmp, "health.json"), "exit_record": os.path.join(self.tmp, "last_exit.json")}`，
并增加（需要 `import json`）：

```python
def test_health_and_exit_record_are_written(self):
    w, cfg = self._run([False] * 5 + [True] * 5)
    health_path = Path(cfg["health"]["path"])
    exit_path = Path(cfg["health"]["exit_record"])
    self.assertTrue(health_path.is_file())
    self.assertGreater(json.loads(health_path.read_text(encoding="utf-8"))["loop_seq"], 0)
    rec = json.loads(exit_path.read_text(encoding="utf-8"))
    self.assertEqual(rec["code"], 0)
    self.assertIn(rec["reason"], ("keyboard", "normal"))
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_health.py`、`python tests/test_integration.py`
Expected: `write_health` 不存在 / `health.json` 不存在。

- [ ] **Step 3: 实现 watcher 接入**

- `Watcher.__init__` 增加上述字段；从 `health` 段读 `path`（默认 `var/state/health.json`）、`snapshot_seconds`（默认 1）、`stall_seconds`（默认 120）。
- `write_health` 调 `health.build_health_payload`；`rss_mb/cpu_pct` 来自 `health.proc_stats()`；额外键 `recording`、`camera_ok`、`desired_state`。
- 主循环顶部 `self.loop_seq += 1`；`self.detect(frame)` 成功后设 `last_inference_at = now`；读帧失败 `consecutive_errors += 1`；成功读帧归零；重连分支 `camera_reopens += 1`；每 `snapshot_seconds` 调一次 `write_health()`。
- `run()` 打开摄像头前启动看门狗线程：
  - `stop_event = threading.Event()`
  - 线程体调 `health.watchdog_loop(get_seq=lambda: self.loop_seq, dump_stacks=self._dump_stacks, on_stall=self._on_stall, sleep=time.sleep, stop_event=stop_event, threshold_seconds=self.stall_seconds)`
  - `_dump_stacks` 用 `faulthandler.dump_traceback(file=self._fault_file, all_threads=True)`；`diagnostics=None` 时退回 `faulthandler.dump_traceback(all_threads=True)`；
  - `_on_stall` 写 `last_exit.json`（`EXIT_STALLED`, `"stalled"`），然后 `os._exit(EXIT_STALLED)`；
  - `finally` 里 `stop_event.set()`。
- 停止请求分支设置 `self.exit_reason = "stop_requested"` 后 `break`。
- `open_camera` 失败改为 `sys.exit(EXIT_CAMERA)`；`load_model` 捕获异常并 `sys.exit(EXIT_MODEL)`；`Watcher.__init__` 的解析错误用 `EXIT_CONFIG`。
- `main`：在监控路径（非 probe/enroll/whoami/status/storage）创建 `Diagnostics`，`install()`；`--log` 用 `logutil.rotating_file_handler`。`main` 只负责把 `SystemExit` 的退出码交给进程，**不写退出记录**。
- `run()`：在现有 `finally` 开头调 `health.write_exit_record(self.exit_record_path, pid=os.getpid(), code=self._exit_code(), reason=self.exit_reason)`；在现有 `except KeyboardInterrupt` 之前加 `except BaseException: self.exit_reason = "unhandled"; raise`。`open_camera`/`load_model` 失败路径先设 `exit_reason` 再 `sys.exit`。`_exit_code()` 映射：`normal`/`stop_requested`/`keyboard`/`file_end` → 0，`config` → 2，`camera` → 3，`model` → 4，`unhandled` → 5，`stalled` → 6。
- 关键事件（启动、停止、摄像头重连、退出）用 `logutil.event` 输出固定字段。
- `Watcher` 接收 `diagnostics` 对象（默认 `None`），用于拿 `fault_file`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_health.py`、`python tests/test_integration.py --fast`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/watcher.py roomwatch/health.py tests/test_health.py tests/test_integration.py
git commit -m "feat(watcher): 健康快照、退出记录与停滞看门狗

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: guardian 记录重启历史与上次退出原因

**Files:**
- Modify: `roomwatch/guardian.py:222-411`、`:440-501`
- Modify: `config.yaml:199-211`、`roomwatch/config.py:31-43`
- Test: `tests/test_guardian.py`

**Interfaces:**
- Consumes: `health.read_exit_record`。
- Produces:
  - `guardian.append_history(path: str | Path, record: dict) -> None`
  - `guardian.read_history(path: str | Path, limit: int = 20) -> list[dict]`
  - `Guard.last_exit_reason() -> str`（读 `last_exit.json`，缺失/损坏返回 `"unknown"`）
  - `Guard.history_path: str`

- [ ] **Step 1: 写失败测试**

```python
class HistoryTest(unittest.TestCase):
    def test_append_and_read_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "h.jsonl"
            G.append_history(p, {"service": "watcher", "action": "restart_gone"})
            G.append_history(p, {"service": "gallery", "action": "restart_gone"})
            rows = G.read_history(p, limit=10)
            self.assertEqual([r["service"] for r in rows], ["watcher", "gallery"])

    def test_missing_exit_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = G.Guard({"guardian": {}, "storage": {"status": str(Path(tmp) / "s.json")},
                         "health": {"exit_record": str(Path(tmp) / "last_exit.json")},
                         "__config_path__": str(Path(tmp) / "c.yaml")}, dry_run=True)
            self.assertEqual(g.last_exit_reason(), "unknown")

    def test_corrupt_exit_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            p.write_text("{oops", encoding="utf-8")
            g = G.Guard({"guardian": {}, "storage": {"status": str(Path(tmp) / "s.json")},
                         "health": {"exit_record": str(p)},
                         "__config_path__": str(Path(tmp) / "c.yaml")}, dry_run=True)
            self.assertEqual(g.last_exit_reason(), "unknown")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_guardian.py`
Expected: `AttributeError: module 'roomwatch.guardian' has no attribute 'append_history'`

- [ ] **Step 3: 实现**

- `config.yaml` 的 `guardian` 段新增：
  `history: var/state/guardian_history.jsonl`、`desired_state: var/state/desired_state.json`、`owner_lock: var/state/guardian.owner.lock`、`gallery_pid: var/state/gallery.pid`、`gallery_log: var/logs/gallery.log`、`supervise_gallery: true`、`crash_window_minutes: 10`、`crash_threshold: 3`、`notify_recovery: true`。
- `config.yaml` 新增 `health` 段：`path: var/state/health.json`、`exit_record: var/state/last_exit.json`、`snapshot_seconds: 1`、`stall_seconds: 120`。
- `config.py` 的 `_PATH_KEYS` 增加对应键。
- `Guard.__init__` 读取上述路径；`last_exit_reason()` 从 `health.exit_record` 指向的文件用 `health.read_exit_record` 取 `reason`，文件缺失或损坏都返回 `"unknown"`。
- guardian 的 `--log` 也改用 `logutil.rotating_file_handler`。
- `check_once` 在重启分支写一条 history：`{"at": now.isoformat(), "service": "watcher", "action": action, "exit_code": rec.get("code"), "exit_reason": rec.get("reason", "unknown")}`。
- `--status` 打印最近退出原因与最近 5 条历史。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_guardian.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/guardian.py roomwatch/config.py config.yaml tests/test_guardian.py
git commit -m "feat(guardian): 记录重启历史与上次退出原因

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: 诊断包 `rw diag`

**Files:**
- Create: `roomwatch/diag.py`
- Create: `tests/test_diag.py`
- Modify: `roomwatch/cli.py:73-104`、`:844-880`
- Modify: `roomwatch/paths.py`（`DIAG_DIR` 已加）
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `paths.PROJECT_ROOT`、`paths.DIAG_DIR`、`cli.run_checks`（在函数内导入，避免循环）。
- Produces:
  - `diag.mask_value(value: str) -> str`
  - `diag.mask_config(data: dict) -> dict`
  - `diag.build_bundle(root: str | Path, out_dir: str | Path | None = None) -> Path`
  - CLI 命令 `diag`

- [ ] **Step 1: 写失败测试**

```python
class MaskTest(unittest.TestCase):
    def test_masks_secret_values_but_keeps_tail(self):
        cfg = {"notify": {"wecom_bot": {"webhook": "https://qyapi.weixin.qq.com/send?key=abcdef123456"},
                          "wxpusher": {"app_token": "AT_123456", "uid": "UID_9999"}}}
        masked = diag.mask_config(cfg)
        self.assertIn("3456", masked["notify"]["wecom_bot"]["webhook"])
        self.assertNotIn("abcdef", masked["notify"]["wecom_bot"]["webhook"])
        self.assertNotIn("123456", masked["notify"]["wxpusher"]["app_token"])

    def test_plain_values_unchanged(self):
        self.assertEqual(diag.mask_config({"camera": {"index": 0}}), {"camera": {"index": 0}})

class BundleTest(unittest.TestCase):
    def test_bundle_contains_masked_local_config_and_no_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var" / "logs").mkdir(parents=True)
            (root / "var" / "logs" / "watcher.log").write_text("hello", encoding="utf-8")
            (root / "config.local.yaml").write_text(
                "notify:\n  wecom_bot:\n    webhook: https://x/send?key=SECRET1234\n", encoding="utf-8")
            out = diag.build_bundle(root)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("var/logs/watcher.log", names)
                self.assertIn("config.local.yaml", names)
                text = zf.read("config.local.yaml").decode("utf-8")
                self.assertNotIn("SECRET1234", text)
                self.assertFalse(any(n.startswith("data/") for n in names))
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_diag.py`
Expected: `ModuleNotFoundError: No module named 'roomwatch.diag'`

- [ ] **Step 3: 实现**

- `mask_config` 递归复制；键名小写后包含 `token`、`secret`、`key`、`webhook`、`password`、`uid`、`heartbeat_url`、`sendkey` 的字符串值走 `mask_value`（长度 ≤4 输出 `***`，否则 `***` + 末 4 位）。
- `build_bundle`：
  - 输出 `DIAG_DIR / f"room-watch-diag-{now:%Y%m%d-%H%M%S}.zip"`；
  - 收 `var/logs/*.log`（单文件最多 2MB，超出取尾部）、`var/state/health.json`、`status.json`、`last_exit.json`、`guardian_state.json`、`guardian_history.jsonl`、`desired_state.json`、`gallery.pid`；
  - `config.yaml` 原样、`config.local.yaml` 经 `mask_config` 后写入；
  - `doctor.txt`（调用 `cli.run_checks`，异常时写错误文本）、`system.txt`（platform、python、版本、根目录）。
  - **不收录** `data/` 下任何文件。
- `cli.py`：`COMMANDS["diag"] = Command("生成诊断包（日志/脱敏配置/健康快照）")`；`main` 里 `if name == "diag": return _cmd_diag(rest)`；`_cmd_diag` 拒绝参数，打印 zip 路径。
- `tests/test_cli.py` 的 `test_all_expected_commands_exist` 增加 `"diag"`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_diag.py`、`python tests/test_cli.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/diag.py roomwatch/cli.py roomwatch/paths.py tests/test_diag.py tests/test_cli.py
git commit -m "feat(diag): 生成脱敏诊断包

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: M0 文档与版本 0.1.11

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: M0 全部实现。
- Produces: 可发布的 M0 修订版。

- [ ] **Step 1: 更新文档**

- `VERSION` → `0.1.11`。
- `CHANGELOG.md` 顶部加一节：健康快照、退出码、崩溃钩子、看门狗、重启历史、`rw diag`。
- `README.md`：命令表加 `diag`；排障表加「看 `var/state/health.json` 与 `last_exit.json`」。
- `AGENTS.md`：F 节加新状态文件与退出码；命令表加 `diag`；目录表加 `health.json`、`last_exit.json`、`guardian_history.jsonl`、`var/diag/`。
- `docs/architecture.md`：模块表加 `health.py`、`diagnostics.py`、`logutil.py`、`diag.py`。
- 在排障文档中说明：原生崩溃转储（WER LocalDumps）需要注册表与权限，**不设为默认**，只给出手动开启步骤。

- [ ] **Step 2: 验证版本一致性**

Run: `python -m roomwatch version`
Expected: 输出包含 `0.1.11`

- [ ] **Step 3: 跑测试**

Run: `python -m roomwatch test --fast`
Expected: 所有文件 `OK`；新增测试全部通过。

- [ ] **Step 4: 提交**

```bash
git add VERSION CHANGELOG.md README.md AGENTS.md docs/architecture.md
git commit -m "docs: M0 可观测性发布 0.1.11

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: 期望状态、gallery PID 与进程身份校验

**Files:**
- Modify: `roomwatch/control.py`
- Modify: `tests/test_control.py`

**Interfaces:**
- Consumes: `procutil.pid_alive`、`procutil.kill_pid`、`health.write_json_atomic`。
- Produces:
  - `control.DESIRED_NAME = "desired_state.json"`
  - `control.desired_state_path(root) -> Path`
  - `control.read_desired_state(root) -> str`（`"running"` / `"paused"`，缺失或损坏一律 `"running"`）
  - `control.set_desired_state(root, state: str, *, by: str = "unknown") -> Path`
  - `control.gallery_pid_path(root) -> Path`
  - `control.pid_matches(pid: int, markers: tuple[str, ...], *, cmdline=None) -> bool`
  - `control.stop_gallery(root, *, alive=None, kill=None, cmdline=None) -> dict`

- [ ] **Step 1: 写失败测试**

```python
class DesiredStateTest(unittest.TestCase):
    def test_missing_file_defaults_to_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(control.read_desired_state(Path(tmp)), "running")

    def test_round_trip_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control.set_desired_state(root, "paused", by="test")
            self.assertEqual(control.read_desired_state(root), "paused")
            control.desired_state_path(root).write_text("{oops", encoding="utf-8")
            self.assertEqual(control.read_desired_state(root), "running")

class PidMatchTest(unittest.TestCase):
    def test_matching_command_line(self):
        cmd = ["python.exe", "-m", "roomwatch", "gallery", "--config", "x"]
        self.assertTrue(control.pid_matches(1, ("roomwatch", "gallery"), cmdline=lambda pid: cmd))

    def test_reused_pid_with_other_command_is_not_a_match(self):
        cmd = ["notepad.exe", "foo.txt"]
        self.assertFalse(control.pid_matches(1, ("roomwatch", "gallery"), cmdline=lambda pid: cmd))

class StopGalleryTest(unittest.TestCase):
    def test_dead_pid_is_already_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = control.stop_gallery(Path(tmp), alive=lambda pid: False)
            self.assertEqual(result["method"], "already-stopped")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_control.py`
Expected: `AttributeError: module 'roomwatch.control' has no attribute 'read_desired_state'`

- [ ] **Step 3: 实现**

- `set_desired_state` 校验取值，写 `{"state": state, "updated_at": ..., "by": by}`，原子写。
- `pid_matches` 默认用 `psutil.Process(pid).cmdline()`；任何异常返回 `False`；所有 marker 都必须出现在命令行里（大小写不敏感）。
- `stop_gallery` 读 `var/state/gallery.pid`（内容为 PID），若 PID 不存在或 `pid_matches` 失败 → `already-stopped`；否则尝试 `kill_pid(pid, force=False)`，失败再 `force=True`；返回 `{"method": "graceful"|"forced"|"already-stopped", "waited": 0.0}`。
- `alive`/`kill`/`cmdline` 全部可注入，测试不碰真实进程。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_control.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/control.py tests/test_control.py
git commit -m "feat(control): 期望状态与 gallery 进程校验

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: gallery 写 PID 文件

**Files:**
- Modify: `roomwatch/gallery.py:439-487`
- Test: `tests/test_gallery.py`（新增纯函数测试）或 `tests/test_control.py`

**Interfaces:**
- Consumes: `paths.STATE_DIR`。
- Produces:
  - `gallery.write_pid_file(path: str | Path, pid: int) -> None`
  - `gallery.remove_pid_file(path: str | Path, pid: int) -> None`（只删内容等于自己的文件）

- [ ] **Step 1: 写失败测试**

```python
class GalleryPidTest(unittest.TestCase):
    def test_write_and_remove_only_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gallery.pid"
            gallery.write_pid_file(p, 123)
            gallery.remove_pid_file(p, 999)
            self.assertTrue(p.exists())
            gallery.remove_pid_file(p, 123)
            self.assertFalse(p.exists())
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_gallery.py`
Expected: `AttributeError: module 'roomwatch.gallery' has no attribute 'write_pid_file'`

- [ ] **Step 3: 实现**

- `gallery.main` 增加 `--pid-file`，默认 `str(STATE_DIR / "gallery.pid")`。
- 服务器成功创建后立刻 `write_pid_file`；`try/finally` 中 `remove_pid_file`。
- 不改变现有 HTTP 行为。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_gallery.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/gallery.py tests/test_gallery.py
git commit -m "feat(gallery): 写 PID 文件供守护进程判活

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: 自启抽象：计划任务优先，Run 键回退

**Files:**
- Modify: `roomwatch/autostart.py`
- Modify: `tests/test_autostart.py`
- Modify: `scripts/install-autostart.ps1`
- Modify: `scripts/room-watch.iss`
- Modify: `scripts/start-guardian.bat`

**Interfaces:**
- Consumes: `winreg`、`subprocess`、`paths.find_python`。
- Produces:
  - `autostart.TASK_NAME = "RoomWatchGuardian"`
  - `autostart.task_xml(root: Path, pythonw: str, config_path: Path, log_path: Path) -> str`
  - `autostart.status(root, *, read=None, runner=None, platform=None) -> dict`，字段增加 `backend`（`task`/`run`/`none`/`conflict`）、`task_name`、`conflict`
  - `autostart.enable(root, python_exe, config_path, *, write=None, delete=None, runner=None, platform=None) -> tuple[bool, str]`
  - `autostart.disable(*, delete=None, runner=None, platform=None) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

```python
def runner_ok(cmd): return (0, "", "")

class TaskSchedulerTest(unittest.TestCase):
    def test_task_xml_has_logon_trigger_and_restart(self):
        xml = autostart.task_xml(ROOT, PYTHONW, ROOT / "config.yaml",
                                 ROOT / "var/logs/guardian.log")
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn("-m roomwatch guard", xml)
        self.assertIn(str(ROOT), xml)

    def test_enable_prefers_task_then_removes_legacy_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            deleted = []
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml", write=lambda v: True,
                                       delete=lambda: deleted.append(True) or True,
                                       runner=runner_ok, platform="win32")
            self.assertTrue(ok, msg)
            self.assertIn("计划任务", msg)
            self.assertTrue(deleted)

    def test_enable_falls_back_to_run_when_task_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            written = {}
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml",
                                       write=lambda v: written.setdefault("v", v) or True,
                                       delete=lambda: True,
                                       runner=lambda cmd: (1, "", "blocked"),
                                       platform="win32")
            self.assertTrue(ok, msg)
            self.assertIn("Run", msg)
            self.assertIn("v", written)

    def test_enable_reports_failure_when_both_paths_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml", write=lambda v: False,
                                       delete=lambda: True,
                                       runner=lambda cmd: (1, "", "blocked"),
                                       platform="win32")
            self.assertFalse(ok)
            self.assertIn("失败", msg)

class BackendStatusTest(unittest.TestCase):
    def test_task_backend(self):
        info = autostart.status(ROOT, read=lambda: None,
                                runner=lambda cmd: (0, "", ""), platform="win32")
        self.assertEqual(info["backend"], "task")

    def test_conflict_when_both_exist(self):
        info = autostart.status(ROOT, read=lambda: autostart.command_line(ROOT),
                                runner=lambda cmd: (0, "", ""), platform="win32")
        self.assertEqual(info["backend"], "conflict")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_autostart.py`
Expected: `AttributeError: module 'roomwatch.autostart' has no attribute 'task_xml'`

- [ ] **Step 3: 实现**

- `task_xml` 生成最小 XML：`LogonTrigger`、`InteractiveToken`、`LeastPrivilege`、`MultipleInstancesPolicy=IgnoreNew`、`DisallowStartIfOnBatteries=false`、`ExecutionTimeLimit=PT0S`、`RestartOnFailure Interval=PT1M Count=3`、Action 用 `pythonw.exe` + `-m roomwatch guard --config <abs> --log <abs>` + `WorkingDirectory`。
- `enable`：先写 VBS；再 `mkdir` 后写 UTF-16 XML 到 `var/state/room-watch-task.xml`，跑 `schtasks /Create /TN RoomWatchGuardian /XML <path> /F`；成功 → 删除 HKCU Run 旧值（迁移）；失败/OSError → 写 Run 值。两条都失败返回 `False, "计划任务与 Run 键都失败：..."`。
- `disable`：先 `schtasks /Delete /TN RoomWatchGuardian /F`，再删 Run 值；任一路径成功即认为成功。
- `status`：task 存在（`schtasks /Query` 返回 0）与 Run 值同时存在 → `backend="conflict"`；只存在其一 → 对应 backend；都没有 → `none`。
- `install-autostart.ps1` 改为调用 `python -m roomwatch autostart --enable`（不再直接 `New-ScheduledTaskAction`）；保留提示与状态输出。
- `room-watch.iss` 的 `[UninstallRun]` 增加：
  `schtasks /Delete /TN RoomWatchGuardian /F`，`RunOnceId: "RoomWatchRemoveTask"`。
- `start-guardian.bat` 改为用 `"%~dp0..\rw.cmd" guard`，去掉硬编码 Python 路径。
- 确认 `wizard.install_autostart()` 仍调用 `autostart.enable()`（已如此）；安装器触发的向导第 4 问默认选择开启。安装器本身不强制自启，保留用户选择。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_autostart.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/autostart.py tests/test_autostart.py scripts/install-autostart.ps1 scripts/room-watch.iss scripts/start-guardian.bat
git commit -m "feat(autostart): 计划任务优先并回退 Run 键

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: guardian 纯决策层：暂停、gallery、崩溃窗口、通知文案

**Files:**
- Modify: `roomwatch/guardian.py`
- Test: `tests/test_guardian.py`

**Interfaces:**
- Consumes: `datetime`、`control.read_desired_state`（在集成层用，纯函数不依赖）。
- Produces:
  - `ACT_PAUSED = "paused"`
  - `decide_gallery(pid: int | None, alive: Callable[[int], bool], *, cmdline=None) -> str`（`ACT_OK` / `ACT_RESTART_GONE`）
  - `crash_window_count(timestamps: list[str], now: datetime, window_minutes: float) -> int`
  - `recovery_text(service: str, exit_record: dict | None) -> str`
  - `crash_alert_text(service: str, count: int, window_minutes: float, exit_record: dict | None) -> str`

- [ ] **Step 1: 写失败测试**

```python
class GalleryDecideTest(unittest.TestCase):
    def test_dead_pid_restarts(self):
        self.assertEqual(G.decide_gallery(None, lambda p: False), G.ACT_RESTART_GONE)
        self.assertEqual(G.decide_gallery(123, lambda p: False), G.ACT_RESTART_GONE)

    def test_alive_matching_pid_is_ok(self):
        cmd = ["python", "-m", "roomwatch", "gallery"]
        self.assertEqual(G.decide_gallery(123, lambda p: True,
                                          cmdline=lambda p: cmd), G.ACT_OK)

    def test_reused_pid_is_restart(self):
        self.assertEqual(G.decide_gallery(123, lambda p: True,
                                          cmdline=lambda p: ["notepad.exe"]),
                         G.ACT_RESTART_GONE)

class CrashWindowTest(unittest.TestCase):
    def test_counts_only_inside_window(self):
        now = datetime(2026, 10, 19, 12, 0, 0)
        stamps = [(now - timedelta(minutes=m)).isoformat() for m in (1, 5, 15)]
        self.assertEqual(G.crash_window_count(stamps, now, 10), 2)

class TextTest(unittest.TestCase):
    def test_recovery_mentions_reason(self):
        text = G.recovery_text("watcher", {"reason": "unhandled", "code": 5})
        self.assertIn("watcher", text)
        self.assertIn("unhandled", text)

    def test_crash_alert_mentions_count(self):
        text = G.crash_alert_text("watcher", 3, 10, {"reason": "unhandled"})
        self.assertIn("3", text)
        self.assertIn("10", text)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_guardian.py`
Expected: `AttributeError: module 'roomwatch.guardian' has no attribute 'decide_gallery'`

- [ ] **Step 3: 实现**

- `decide_gallery`：`pid` 为空/不活 → restart；`cmdline` 提供且不包含 `roomwatch` 与 `gallery` → restart；否则 ok。
- `crash_window_count`：解析 ISO 时间，统计 `now - t <= window_minutes*60` 的条数；坏时间跳过。
- 文案函数只负责纯字符串，不发送。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_guardian.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/guardian.py tests/test_guardian.py
git commit -m "feat(guardian): 暂停、gallery 与崩溃窗口决策

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: guardian 集成：暂停、守护 gallery、通知与单实例

**Files:**
- Modify: `roomwatch/guardian.py`
- Modify: `config.yaml`（若 Task 4 未加全）
- Test: `tests/test_guardian.py`

**Interfaces:**
- Consumes: Task 4/7/8/10 的接口。
- Produces:
  - `Guard.check_once()` 返回新增 `ACT_PAUSED`
  - `Guard._start_gallery() -> Optional[int]`
  - `Guard._notify(text: str) -> None`（后台线程，失败只记日志）
  - `Guard.run_forever()` 生命周期锁

- [ ] **Step 1: 写失败测试**

用临时目录与 mock：

```python
class GuardPausedTest(unittest.TestCase):
    def test_paused_does_not_start_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"guardian": {"desired_state": str(root / "var/state/desired_state.json"),
                                "state": str(root / "var/state/guardian_state.json"),
                                "lock": str(root / "var/state/guardian.lock"),
                                "history": str(root / "var/state/h.jsonl"),
                                "exit_record": str(root / "var/state/last_exit.json")},
                   "storage": {"status": str(root / "var/state/status.json")},
                   "__config_path__": str(root / "config.yaml")}
            control.set_desired_state(root, "paused")
            g = G.Guard(cfg, dry_run=True)
            with mock.patch.object(g, "_start_watcher") as sw, \
                 mock.patch.object(g, "_start_gallery") as sg:
                self.assertEqual(g.check_once(), G.ACT_PAUSED)
                sw.assert_not_called(); sg.assert_not_called()

class GuardRecoveryTest(unittest.TestCase):
    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "gallery_pid": str(root / "var/state/gallery.pid"),
                    "gallery_log": str(root / "var/logs/gallery.log")},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_recovery_notification_sent_once_after_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["pending_recovery"] = {"service": "watcher", "reason": "unhandled"}
            sent = []
            g._notify = lambda text: sent.append(text)
            self.assertEqual(g.check_once(), G.ACT_OK)
            self.assertEqual(len(sent), 1)
            self.assertIn("unhandled", sent[0])
            g.check_once()
            self.assertEqual(len(sent), 1)

    def test_selfcheck_failure_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            (root / "var/state/health.json").write_text(json.dumps({
                "selfcheck": [{"name": "推送", "ok": False, "detail": "未配置"}]}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            sent = []
            g._notify = lambda text: sent.append(text)
            g.check_once()
            self.assertTrue(any("自检" in s for s in sent))
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_guardian.py`
Expected: 暂停时仍尝试启动 / 无 `ACT_PAUSED`。

- [ ] **Step 3: 实现**

- `Guard.__init__` 读 `desired_state`、`gallery_pid`、`gallery_log`、`supervise_gallery`、`notify_recovery`、`crash_window_minutes`、`crash_threshold`。
- `check_once`：
  1. `if control.read_desired_state(root) == "paused":` 清 `pending_recovery`、`stall_streak`、保存状态，返回 `ACT_PAUSED`；**不**启动/重启任何服务。
  2. watcher 走现有逻辑；重启时读 `last_exit_reason()`，写历史，设置 `pending_recovery={"service":"watcher","reason":..., "at":...}`，按崩溃窗口决定是否 `_notify(crash_alert_text(...))`。
  3. `ACT_OK` 且 `pending_recovery` 非空且 `notify_recovery` → `_notify(recovery_text(...))`，清除。
  4. `supervise_gallery`：读 `gallery.pid`，`decide_gallery`；需要重启时 `_start_gallery()`，写历史。
  5. `ACT_OK` 时读 `health.json` 的 `selfcheck`；有失败项且距上次告警超过 10 分钟时 `_notify("启动自检失败：...")`，记录 `last_selfcheck_alert_at`。
- 测试文件需要补 `import json`。
- `_start_gallery`：`[python, "-m", "roomwatch", "gallery", "--config", config_path, "--pid-file", gallery_pid]`，stdout/stderr 重定向到 `gallery_log`，`DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`；返回 PID。
- `_notify`：`threading.Thread(target=send, daemon=True).start()`；`send` 构造 `Notifier(cfg.get("notify") or {})` 并 `send(text)`；异常只 `log.warning`。
- `run_forever`：启动时 `FileLock(owner_lock).acquire()`；拿不到则 `return`；退出时 `release()`。
- `--status` 增加：期望状态、gallery PID/存活、最近退出原因、最近 5 条历史。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_guardian.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/guardian.py config.yaml tests/test_guardian.py
git commit -m "feat(guardian): 暂停语义、gallery 守护与恢复通知

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 12: CLI 启停语义与状态扩展

**Files:**
- Modify: `roomwatch/cli.py`
- Modify: `roomwatch/watcher.py` 的 `print_status`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `control.read_desired_state`、`control.set_desired_state`、`control.stop_gallery`、`paths.find_python`。
- Produces:
  - `rw start`：写 `running`，确保 guardian 存在，并触发一次 `guard --once`；`rw start --foreground` 保持旧行为。
  - `rw stop`：写 `paused`，停止 gallery 与 watcher。
  - `rw status`：增加期望状态、最近退出原因。

- [ ] **Step 1: 写失败测试**

```python
class StartStopSemanticsTest(unittest.TestCase):
    def test_start_foreground_passes_through(self):
        with mock.patch.object(cli, "_run_module", return_value=0) as run:
            self.assertEqual(cli._cmd_start(["--foreground", "--config", "x"]), 0)
        run.assert_called_once_with("watcher", ["--config", "x"])

    def test_stop_sets_paused_before_stopping(self):
        from roomwatch import control
        order = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli.paths, "PROJECT_ROOT", Path(tmp)), \
             mock.patch.object(control, "set_desired_state",
                               side_effect=lambda *a, **k: order.append("state")) as setstate, \
             mock.patch.object(control, "stop_gallery",
                               side_effect=lambda *a, **k: order.append("gallery") or
                               {"method": "already-stopped"}) as sg, \
             mock.patch.object(cli, "_status_pid", return_value=0):
            cli._cmd_stop([])
        setstate.assert_called_once()
        sg.assert_called_once()
        self.assertEqual(order, ["state", "gallery"])

    def test_start_is_registered_without_module(self):
        self.assertEqual(cli.COMMANDS["start"].module, "")

    def test_command_table_includes_diag(self):
        self.assertIn("diag", cli.COMMANDS)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_cli.py`
Expected: `start` 仍指向 watcher；`_cmd_start` 不存在。

- [ ] **Step 3: 实现**

- `COMMANDS["start"]` 改为无 module 的说明；`main` 增加 `if name == "start": return _cmd_start(rest)`。
- `_cmd_start`：
  - `--foreground` → `_run_module("watcher", [a for a in rest if a != "--foreground"])`；
  - 否则 `control.set_desired_state(root, "running", by="cli")`；用 `subprocess.Popen`（DETACHED）启动 `python -m roomwatch guard --config ... --log ...`；再同步运行 `python -m roomwatch guard --once ...`；打印「已启动」。
- `_cmd_stop`：先 `set_desired_state(root, "paused", by="cli")`，再 `stop_gallery`，再 `stop_watcher`；输出两者结果。
- `watcher.print_status` 读取 `desired_state.json` 与 `last_exit.json`，打印「期望状态」「上次退出」；读不到不报错。
- 更新 `tests/test_cli.py` 现有 `translate("start", ...)` 断言。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_cli.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/cli.py roomwatch/watcher.py tests/test_cli.py
git commit -m "feat(cli): 启停走期望状态与守护进程

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 13: GUI 启停、暂停状态与健康提示

**Files:**
- Modify: `roomwatch/gui.py:102-157`（`read_state`）、`:229-241`（命令构造）、`:618-660`（启停）
- Test: `tests/test_gui_state.py`

**Interfaces:**
- Consumes: `control.read_desired_state`、`control.set_desired_state`、`control.stop_gallery`、`health.read_json`。
- Produces:
  - `gui.guard_command(python: str, root: Path, config_path: str, *, once: bool = False) -> list[str]`
  - `read_state` 新 phase `"paused"`；caption 追加上次退出原因与自检失败摘要。

- [ ] **Step 1: 写失败测试**

```python
class PausedStateTest(unittest.TestCase):
    def test_paused_and_offline_is_paused_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control.set_desired_state(root, "paused")
            state = gui.read_state(root, alive=DEAD)
        self.assertEqual(state.phase, "paused")
        self.assertIn("暂停", state.caption)

    def test_last_exit_reason_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/last_exit.json").write_text(
                '{"code": 5, "reason": "unhandled"}', encoding="utf-8")
            state = gui.read_state(root, alive=DEAD)
        self.assertIn("unhandled", state.caption)

class GuardCommandTest(unittest.TestCase):
    def test_guard_command_layout(self):
        cmd = gui.guard_command("C:/py/python.exe", Path("C:/proj"), "C:/proj/config.yaml")
        self.assertIn("guard", cmd)
        self.assertIn("C:/proj/config.yaml", cmd)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_gui_state.py`
Expected: `phase` 仍为 `stopped` / `guard_command` 不存在。

- [ ] **Step 3: 实现**

- `read_state`：
  - 读 `desired_state.json`；离线且 `paused` → `phase="paused"`、caption `"已暂停（守护不会自动重启）"`；
  - 离线且非 paused → 读 `last_exit.json`，caption 追加 `"（上次退出：<reason>）"`；
  - 读 `health.json` 的 `selfcheck`，有失败项时 caption 追加 `" · 自检失败：<name>"`。
- `guard_command` 返回 `[python, "-m", "roomwatch", "guard", "--config", config]`，`once=True` 时加 `--once`；`--log` 指向 `var/logs/guardian.log`。
- `on_start`：`control.set_desired_state` → `_spawn(guard_command(...))` + `_spawn(guard_command(..., once=True))`；不再直接 `start_command`。
- `on_stop`：先 `set_desired_state(paused)`，再 `stop_gallery`，再现有 `stop_watcher` 流程。
- `button_states`：`phase=="paused"` 与 `stopped` 一致（启动可用、停止禁用）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_gui_state.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/gui.py tests/test_gui_state.py
git commit -m "feat(gui): 暂停状态与守护进程启停

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 14: 启动自检模块并接入 watcher

**Files:**
- Create: `roomwatch/selfcheck.py`
- Create: `tests/test_selfcheck.py`
- Modify: `roomwatch/watcher.py`（`run()` 入口）
- Modify: `roomwatch/health.py`（如需 `CheckResult` 序列化辅助）

**Interfaces:**
- Consumes: `paths.resolve`、`config` 中的模型/推送/存储键。
- Produces:
  - `selfcheck.CheckResult(name: str, ok: bool, detail: str)`
  - `selfcheck.run(cfg: dict, root: str | Path, *, free_bytes=None) -> list[CheckResult]`
  - `Watcher.selfcheck: list[dict]`（写入 health）

- [ ] **Step 1: 写失败测试**

```python
class SelfcheckTest(unittest.TestCase):
    def test_missing_model_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"detector": {"model": "nope.pt"}, "notify": {"channel": "none"}}
            results = selfcheck.run(cfg, tmp)
            self.assertTrue(any(r.name == "模型" and not r.ok for r in results))

    def test_placeholder_webhook_fails_push(self):
        cfg = {"notify": {"channel": "wecom_bot",
                          "wecom_bot": {"webhook": "https://x?key=REPLACE_ME"}}}
        results = selfcheck.run(cfg, ".")
        push = next(r for r in results if r.name == "推送")
        self.assertFalse(push.ok)

    def test_configured_push_and_models_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "models").mkdir()
            (root / "models" / "y.pt").write_bytes(b"x")
            (root / "models" / "f.onnx").write_bytes(b"x")
            cfg = {"detector": {"model": str(root / "models/y.pt")},
                   "face": {"model": str(root / "models/f.onnx")},
                   "notify": {"channel": "wxpusher",
                              "wxpusher": {"app_token": "AT_REAL", "uid": "UID_REAL"}},
                   "storage": {"min_free_gb": 0}}
            results = selfcheck.run(cfg, root)
            self.assertTrue(all(r.ok for r in results),
                            [r for r in results if not r.ok])

    def test_low_disk_fails(self):
        results = selfcheck.run({}, ".", free_bytes=0)
        self.assertTrue(any(r.name == "磁盘" and not r.ok for r in results))

    def test_result_is_json_serializable(self):
        json.dumps([{"name": r.name, "ok": r.ok, "detail": r.detail}
                    for r in selfcheck.run({}, ".")])
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python tests/test_selfcheck.py`
Expected: `ModuleNotFoundError: No module named 'roomwatch.selfcheck'`

- [ ] **Step 3: 实现**

- 检查项：`模型`（`detector.model` 与 `detector.onnx_model`、`face.model` 存在；`identity.owner_file` 未注册只记提示，不算失败）、`目录`（`data/captures`、`var/state`、`var/logs`、`var/db` 可写）、`推送`（渠道非 none 且凭据不含 `replace_me`/`REPLACE`）、`磁盘`（`shutil.disk_usage(root).free >= storage.min_free_gb`）。
- `run` 不抛异常；每项失败把可操作原因写进 `detail`。
- `Watcher.run()` 在 `open_camera()` 之前调用 `selfcheck.run`，结果存 `self.selfcheck` 并写日志（失败项 `log.warning`）；摄像头打开成功后再写一次 health。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python tests/test_selfcheck.py`
Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add roomwatch/selfcheck.py roomwatch/watcher.py roomwatch/health.py tests/test_selfcheck.py
git commit -m "feat(selfcheck): 启动自检并写入健康快照

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 15: M1 文档与版本 0.1.12

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: M1 全部实现。
- Produces: 可发布的 M1 修订版。

- [ ] **Step 1: 更新文档**

- `VERSION` → `0.1.12`。
- `CHANGELOG.md`：暂停语义、gallery 守护、计划任务/ Run 回退、恢复通知、崩溃阈值、自检、`rw start`/`rw stop` 行为变化。
- `README.md`：说明 `rw start` 现在是后台启动并交由 guardian 托管，`--foreground` 保留旧行为；`rw stop` 同时停止 gallery；自启可能走计划任务或 Run 键；电脑需保持登录。
- `AGENTS.md`：更新命令表、启停语义、desired_state/owner_lock/gallery.pid 的作用、F 节新增「暂停后 guardian 不得拉起」。
- `docs/architecture.md`：guardian 职责改为守护 watcher + gallery；增加 `selfcheck.py`。

- [ ] **Step 2: 跑全量测试**

Run: `python -m roomwatch test`
Expected: 所有测试文件 `OK`，包括集成测试。

- [ ] **Step 3: 验证版本**

Run: `python -m roomwatch version`
Expected: 输出包含 `0.1.12`

- [ ] **Step 4: 提交**

```bash
git add VERSION CHANGELOG.md README.md AGENTS.md docs/architecture.md
git commit -m "docs: M1 守护自愈发布 0.1.12

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 完成标准（M0–M1）

- 人为制造未处理异常、摄像头失败、停滞时，`var/state/last_exit.json` 与 `var/logs/watcher-fault.log` 能说明原因。
- `rw diag` 生成不含原始凭据与房间图片的 zip。
- 杀掉 watcher 后 guardian 自动拉起并写历史；杀掉 gallery 后同样恢复。
- 用户执行 `rw stop` 后，guardian 不再拉起任何服务；`rw start` 恢复且只启动一个 watcher。
- 计划任务被拦截的机器上，自启回退到 HKCU Run + VBS，且不会出现两套自启同时运行。
- `python -m roomwatch test` 全绿；现有配置无需修改即可升级。
