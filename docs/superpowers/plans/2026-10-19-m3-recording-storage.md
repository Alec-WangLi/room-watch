# M3 录制/存储 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「录到什么时候、什么时候不该录、还能录多久」变成可配置、可观测、可预测的能力：录像时长策略进设置界面，本人在电脑前时按明确策略处理，硬件编码先测量再决定，存储给出容量预测和预警。

**Architecture:** 纯逻辑（策略判定、存储预测）放 `rules.py`；设置后端 `settings.py` 暴露新字段并做跨字段校验；watcher 读取策略并在开录/推送两个闸门上执行；新增独立基准工具 `benchcodec.py` + `rw bench-encode`，只测量、不改变默认编码器；存储预测同时供 `rw storage`、`status.json`、主界面和设置页使用。

**Tech Stack:** Python 3.13/3.14 标准库、OpenCV `VideoWriter`、ffmpeg（可选，仅基准用）、SQLite 无关；unittest；Tkinter 设置界面。

**Spec:** `docs/handoff/2026-10-19-session.md` 第 4 节「M3 录制/存储」；`AGENTS.md` I.2。

## Global Constraints

- Windows 10/11 x64；USB 摄像头只能在登录会话采集；不引入 Windows 服务。
- 只动修订号：本计划目标版本 `0.1.17`；未经所有者同意不发 tag / Release。
- 新功能先写失败测试；合并前跑全量 `rw test`（含集成测试）；新增测试必须离线，不依赖真实摄像头、网络或外部服务。
- 稳定性 > 性能 > 功能数量。**不改变默认录像编码器**：硬件编码只做基准，是否切换由数据说话、由所有者拍板。
- 本人在电脑前的默认策略保持现状（照录、不推送），避免为了省磁盘而漏掉真正的闯入者。
- 设置只写 `config.local.yaml`，写完复读校验；密钥不回显。
- 主循环不得因存储统计/基准逻辑而变慢或阻塞；存储预测每 `storage.check_minutes` 最多算一次。
- 配置向后兼容：旧 `behavior.skip_when_user_active` 继续生效。

## 产品决策（本次实现采取的默认值）

所有者选择「直接按路线图开始」，因此以下取舍由实现者先定，写在这里便于复核：

1. **本人在场策略默认 `mute`**：键盘/鼠标活动时照常抓拍和录像，只抑制推送——与 0.1.16 行为一致。`skip`（不录像、不推送）为显式选项，供愿意用存储换隐私的人开启。
2. **`skip` 只停录像与推送，抓拍仍保留**：抓拍只有几百 KB，却是「有人进来过」的最终证据；为了省磁盘把证据一起丢掉不符合「真出事时一定通知到」。
3. **策略只在开录时生效，不打断进行中的录像**：如果正在录别人时本人坐下，已经录到的证据不能因为策略切换被掐断。
4. **硬件编码结论只记录、不自动切换**：本版交付基准工具与实测数据；是否换编码器另开版本。
5. **存储预警不默认推送**：只在日志、`rw storage`、主界面与设置页显示，避免每天一条「磁盘快满了」变成噪音。

---

### Task 1: 存储预测纯逻辑

**Files:**
- Modify: `roomwatch/rules.py`
- Test: `tests/test_logic.py`

**Interfaces:**
- Consumes: 现有 `iter_media_files(base_dir) -> [(mtime, path, size)]`。
- Produces:
  - `STORAGE_UNLIMITED/OK/WARNING/OVER` 常量。
  - `storage_forecast(files, now=None, max_bytes=0, keep_days=0, warn_days=7, window_days=7) -> dict`：
    返回 `used_bytes / file_count / daily_bytes / days_to_cap / days_to_keep_expiry / projected_days / status / oldest_at / newest_at`。
  - 增长率算法：按文件 `mtime` 的日历日期分组，取最近 `window_days` 天里**不含今天**的完整天的平均；没有完整天时用今天的累计量；都为 0 时 `daily_bytes=0`、`days_to_cap=None`。

- [ ] **Step 1: 写失败测试**

```python
class StorageForecastTest(unittest.TestCase):
    def test_empty_directory_is_ok(self):
        f = storage_forecast([], now=NOW, max_bytes=10**9, keep_days=30)
        self.assertEqual(f["status"], STORAGE_OK)
        self.assertEqual(f["daily_bytes"], 0)
        self.assertIsNone(f["days_to_cap"])

    def test_daily_growth_uses_complete_days_not_today(self):
        # 昨天 500MB、前天 500MB、今天 5GB -> daily_bytes 按 500MB 算，不被今天拉高
    def test_today_only_falls_back_to_today_usage(self):
        # 只有今天的 300MB -> daily_bytes == 300MB
    def test_days_to_cap_is_remaining_over_daily(self):
        # used 8GB, max 10GB, daily 1GB -> 2.0 天
    def test_over_cap_reports_over(self):
        # used > max -> status OVER, days_to_cap 0
    def test_no_cap_no_keep_is_unlimited(self):
        # max_bytes=0, keep_days=0 -> status UNLIMITED, projected_days None
    def test_warning_when_projected_below_warn_days(self):
        # projected 3 天, warn_days 7 -> WARNING
    def test_keep_days_limits_projection_even_with_big_disk(self):
        # max 很大但 keep_days=5 -> projected_days 5，status 取决于 warn_days
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/test_logic.py`
Expected: FAIL with `ImportError: cannot import name 'storage_forecast'`.

- [ ] **Step 3: 实现 `storage_forecast`**

要求：
- 纯函数，`now` 可注入；不读磁盘、不调用 `shutil`。
- 日期分组用 `datetime.fromtimestamp(mtime).date()`；窗口从 `now.date()` 往前数。
- `days_to_cap = round((max_bytes - used) / daily, 1)`，`used >= max` 时为 `0.0`。
- `projected_days = min(可用值)`；无任何限制时为 `None`。
- 状态优先级：`OVER > WARNING > OK/UNLIMITED`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/test_logic.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add roomwatch/rules.py tests/test_logic.py
git commit -m "feat(storage): forecast daily growth and days until full"
```

---

### Task 2: 本人在场策略纯逻辑

**Files:**
- Modify: `roomwatch/rules.py`
- Test: `tests/test_logic.py`

**Interfaces:**
- Produces:
  - `OWNER_KEEP/OWNER_MUTE/OWNER_SKIP` 常量与 `OWNER_POLICIES`。
  - `normalize_owner_policy(raw, legacy_skip=None) -> str`：合法值原样返回；非法/缺失时按 `legacy_skip` 映射（True→mute，False→keep），再缺省为 mute。
  - `allow_record(policy, user_active) -> bool`：`skip` 且活跃 → False，其余 True。
  - `allow_notify(policy, user_active) -> bool`：`keep` 或未活跃 → True；`mute`/`skip` 且活跃 → False。

- [ ] **Step 1: 写失败测试**

```python
class OwnerPolicyTest(unittest.TestCase):
    def test_normalize_accepts_known_values(self):
    def test_legacy_true_maps_to_mute(self):
    def test_legacy_false_maps_to_keep(self):
    def test_unknown_falls_back_to_mute(self):
    def test_keep_records_and_notifies_while_active(self):
    def test_mute_records_but_does_not_notify(self):
    def test_skip_records_and_notifies_nothing_while_active(self):
    def test_policy_only_applies_when_user_active(self):
```

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_logic.py`
Expected: FAIL with import error.

- [ ] **Step 3: 实现策略函数**

要求：纯函数、无 I/O；`user_active` 由调用方判断（键鼠空闲阈值）。

- [ ] **Step 4: 运行测试通过**

Run: `python tests/test_logic.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add roomwatch/rules.py tests/test_logic.py
git commit -m "feat(record): owner-at-computer policy decisions"
```

---

### Task 3: watcher 接入策略与存储预测

**Files:**
- Modify: `roomwatch/watcher.py`
- Test: `tests/test_logic.py`（纯逻辑已覆盖）；`tests/test_gui_state.py`（状态字段）；新增 `tests/test_watcher_policy.py`（可选，若现有 watcher 测试难以注入）

**Interfaces:**
- Consumes: `normalize_owner_policy`、`allow_record`、`allow_notify`、`storage_forecast`。
- Produces:
  - Watcher 读取 `record.owner_at_computer`；缺失时从 `behavior.skip_when_user_active` 推导。
  - `user_is_active()` 只回答键鼠是否活跃，不再包含策略判断。
  - `record_allowed()`：`allow_record(self.owner_policy, self.user_is_active())`。
  - `notify_allowed()`：`not dry_run and allow_notify(...)`。
  - `start_recording()` 在 `record_allowed()` 为 False 时直接返回（不启动录像）。
  - `enforce_storage()` 每轮更新 `self.storage_forecast`；`status.json` 增加：
    `storage_used_gb`、`storage_max_gb`、`storage_days_left`、`storage_status`。
  - 预警状态（`warning`/`over`）每个自然日最多写一条日志。

- [ ] **Step 1: 写失败测试**

```python
class WatcherPolicyTest(unittest.TestCase):
    def test_skip_policy_does_not_start_recording_while_user_active(self):
        # 注入 user_is_active=True、owner_at_computer=skip；ACTION_START 后 recorder.recording False
    def test_mute_policy_starts_recording_but_notifies_nothing(self):
        # recorder.recording True；outbox pending 不增加
    def test_keep_policy_starts_recording_and_notifies(self):
    def test_policy_does_not_stop_in_flight_recording(self):
        # 已在录像 -> 策略变为 skip -> pump_recording 不调用 stop
    def test_status_includes_storage_forecast(self):
        # write_status 后 status.json 含 storage_status/storage_days_left
```

注意：测试必须 mock 摄像头、检测器、身份与编码器；不联网、不碰真实设备。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_watcher_policy.py`
Expected: FAIL（属性不存在或行为不符）。

- [ ] **Step 3: 实现接入**

- `__init__`：`self.owner_policy = normalize_owner_policy(rec_cfg.get("owner_at_computer"),
  legacy_skip=behavior.get("skip_when_user_active"))`。
- `start_recording`：开头加 `if not self.record_allowed(): log.info("本人在电脑前，按策略不录像"); return`。
- `enforce_storage`：在清理后计算 `files` 与 `storage_forecast`；保存 `self.storage_forecast`；写每日一次日志。
- `write_status`：加入 4 个存储字段（GB 保留两位小数）。

- [ ] **Step 4: 运行相关测试**

Run: `python tests/test_watcher_policy.py tests/test_gui_state.py tests/test_logic.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add roomwatch/watcher.py tests/test_watcher_policy.py tests/test_gui_state.py
git commit -m "feat(watcher): owner policy and storage forecast in status"
```

---

### Task 4: 配置与设置后端（录像与存储字段）

**Files:**
- Modify: `config.yaml`, `roomwatch/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces 新配置键（带注释）：
  - `record.owner_at_computer: mute`（keep/mute/skip）
  - `record.max_seconds`、`record.max_total_seconds`、`record.min_seconds`、`record.idle_stop_seconds`（已有，补进设置 SPEC）
  - `storage.warn_days: 7`
  - 复用 `storage.max_total_gb`、`snapshot.keep_days`
- `settings.SPEC` 新增「录像」页字段：
  - `record.enabled`（bool）
  - `record.min_seconds`（int 5–600）
  - `record.idle_stop_seconds`（int 5–600）
  - `record.max_seconds`（int 30–3600）
  - `record.max_total_seconds`（int 60–86400）
  - `record.owner_at_computer`（choice）
  - `storage.max_total_gb`（int 0–1000，0=不限）
  - `storage.warn_days`（int 1–90）
  - `snapshot.keep_days`（int 0–3650）
- `sections()` 增加 `"录像"`（顺序：摄像头/推送/相册/身份/录像/心跳/自启/声音）。
- 跨字段校验（仅本次改动涉及 record 时）：`min_seconds <= max_seconds <= max_total_seconds`；
  `max_total_seconds >= max_seconds`；错误信息中文、指明字段。
- `snapshot()` 增加 `storage` 摘要（`settings.storage_info(root, cfg)`）：
  `used_bytes/max_bytes/free_bytes/keep_days/warn_days/forecast`，供设置页与主界面使用。
- `LOCAL_HEADER` 里「录像时长这类参数请改 config.yaml」改为指向设置页。

- [ ] **Step 1: 写失败测试**

```python
class RecordSettingsTest(unittest.TestCase):
    def test_eight_sections_include_record(self):
        self.assertEqual(len(settings.sections()), 8)
        self.assertIn("录像", settings.sections())
    def test_record_fields_are_in_spec(self):
    def test_owner_policy_choice_is_validated(self):
        # "pause" -> 中文错误；"skip" 合法
    def test_max_seconds_must_not_exceed_total(self):
        # max_seconds=3600, max_total_seconds=1800 -> ok False
    def test_min_seconds_must_not_exceed_max(self):
    def test_values_are_written_to_local_and_reread(self):
    def test_snapshot_reports_storage_summary(self):
```

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_settings.py`
Expected: FAIL（7 != 8、字段缺失）。

- [ ] **Step 3: 实现**

- `settings.py` 顶部加字段；`storage_info` 用 `rules.iter_media_files` + `storage_forecast`，目录不存在时返回零值。
- 跨字段校验放在 `apply` 的相册校验旁边，只在 `coerced` 含 `record.` 键时触发。
- `config.yaml` 在 `record` 段加 `owner_at_computer: mute` 与注释；`storage` 段加 `warn_days: 7`。

- [ ] **Step 4: 运行测试通过**

Run: `python tests/test_settings.py tests/test_config.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add config.yaml roomwatch/settings.py tests/test_settings.py
git commit -m "feat(settings): record policy and storage budget fields"
```

---

### Task 5: 设置界面「录像」页与主界面存储预警

**Files:**
- Modify: `roomwatch/gui_settings.py`, `roomwatch/gui.py`
- Test: `tests/test_settings.py`、`tests/test_gui_state.py`；用 `var/render/render.py` 渲染检查

**Interfaces:**
- Consumes: `settings.snapshot()["storage"]`、`settings.sections()`。
- Produces:
  - `SECTIONS` 8 页，顺序与后端一致。
  - 「录像」页顶部显示存储摘要：`当前占用 X GB / 上限 Y GB · 预计还够 Z 天`；
    `warning/over` 时用警示色并显示「按当前增长，N 天后达到上限」或「已超上限」。
  - 主界面 `MonitorState` 增加 `storage_status/storage_days_left/storage_used_gb`，
    底栏在 `warning/over` 时显示「存储预计 N 天后满」（无数据不显示）。
  - 设置页仍走 `rw config --json` / `rw config-set` 子进程，不 import yaml。

- [ ] **Step 1: 写失败测试**

```python
class SettingsTabsTest(unittest.TestCase):
    def test_window_tabs_match_backend_sections(self):
        self.assertEqual(tuple(settings.sections()), gui_settings.SECTIONS)
        self.assertEqual(len(gui_settings.SECTIONS), 8)

class GuiStorageStateTest(unittest.TestCase):
    def test_storage_warning_is_shown(self):
        # status.json: storage_status=warning, storage_days_left=2.4
        self.assertIn("存储", state.caption)  # 或对应的状态文案字段
    def test_no_storage_warning_when_ok(self):
```

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_settings.py tests/test_gui_state.py`
Expected: FAIL.

- [ ] **Step 3: 实现界面**

- `gui_settings._build_storage_banner(frame)`：读取 snapshot 的 storage 摘要；
  数据缺失时显示「存储统计将在监控运行后出现」。
- `gui.read_state` 解析新字段；`window` 底栏拼接预警文案（不要覆盖自检/上次退出提示）。
- 设置窗口高度按需微调，保证 8 页都不裁切。

- [ ] **Step 4: 运行测试并渲染**

Run: `python tests/test_settings.py tests/test_gui_state.py`
Expected: PASS

Run: `python var/render/render.py`（若需要，先更新为 8 页）
Expected: 逐页截图无右边缘裁切、中文完整。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/gui_settings.py roomwatch/gui.py tests/test_settings.py tests/test_gui_state.py
git commit -m "feat(gui): record page and storage warning"
```

---

### Task 6: 编码基准工具与 CLI

**Files:**
- Create: `roomwatch/benchcodec.py`
- Modify: `roomwatch/cli.py`
- Test: `tests/test_benchcodec.py`

**Interfaces:**
- Produces:
  - `CV2_CODECS = ("avc1", "mp4v", "MJPG")`。
  - `FFMPEG_HW_CODECS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf")`。
  - `summarize(name, frames, seconds, size_bytes) -> dict`（`fps/ms_per_frame/mb_per_min`）。
  - `parse_encoder_list(text) -> set[str]`：从 `ffmpeg -encoders` 输出解析编码器名。
  - `available_hw_encoders(ffmpeg) -> list[str]`：只返回候选里可用的；ffmpeg 不存在返回 `[]`。
  - `bench_cv2(codec, frames, size, fps, tmpdir, writer_factory=None) -> dict`。
  - `bench_ffmpeg(ffmpeg, encoder, frames, size, fps, tmpdir, runner=None) -> dict`。
  - `run_all(...) -> list[dict]`。
  - `rw bench-encode [--frames N] [--size 1280x720] [--fps N] [--json] [--with-ffmpeg/--no-ffmpeg]`。
- 基准输出字段：`encoder / kind(cv2|ffmpeg) / ok / fps / ms_per_frame / mb_per_min / file_mb / error`。

- [ ] **Step 1: 写失败测试**

```python
class SummarizeTest(unittest.TestCase):
    def test_math_is_consistent(self):
        s = summarize("avc1", frames=100, seconds=2.0, size_bytes=10 * 1024**2)
        self.assertAlmostEqual(s["fps"], 50.0)
        self.assertAlmostEqual(s["ms_per_frame"], 20.0)
        self.assertAlmostEqual(s["mb_per_min"], 300.0)
    def test_zero_seconds_does_not_divide_by_zero(self):

class EncoderListTest(unittest.TestCase):
    def test_parses_ffmpeg_encoders_output(self):
        text = " V....D h264_nvenc  NVIDIA NVENC H.264 encoder\n V....D libx264 ..."
        self.assertIn("h264_nvenc", parse_encoder_list(text))
    def test_missing_ffmpeg_returns_empty(self):
        # runner 抛 FileNotFoundError -> []

class RunAllTest(unittest.TestCase):
    def test_one_failed_encoder_does_not_abort_others(self):
        # 注入 runner：avc1 成功、mp4v 抛异常 -> 两条结果，失败项 ok=False
    def test_json_shape_is_stable(self):
```

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_benchcodec.py`
Expected: FAIL with import error.

- [ ] **Step 3: 实现**

- cv2 路径：合成有明显运动的帧（参考 `tests/test_recorder.py` 的 `synthetic_frame`）；
  每帧 `writer.write`，计时 `time.perf_counter()`；收尾 `release()` 后取文件大小。
- ffmpeg 路径：`Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
  "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
  "-c:v", encoder, "-pix_fmt", "yuv420p", "-y", out])`，逐帧写 stdin，close 后 wait。
- 编码器不可用时捕获异常并返回 `ok=False, error=...`，不影响其他项。
- `cli.py` 注册 `bench-encode`：`Command("测量录像编码速度（不改默认编码器）")`，主分支调用 `benchcodec.main(rest)`。

- [ ] **Step 4: 运行测试通过**

Run: `python tests/test_benchcodec.py tests/test_cli.py`
Expected: PASS

- [ ] **Step 5: 本机实测并记录**

Run: `python -m roomwatch bench-encode --frames 120 --size 1280x720 --fps 10`
Expected: 输出每个可用编码器的 fps / ms/帧 / MB/分钟；把结论写进 Task 8 的 CHANGELOG 与交接文档。

- [ ] **Step 6: 提交**

```bash
git add roomwatch/benchcodec.py roomwatch/cli.py tests/test_benchcodec.py
git commit -m "feat(bench): encoder benchmark for cv2 and ffmpeg hardware codecs"
```

---

### Task 7: `rw storage` 输出预测与 JSON

**Files:**
- Modify: `roomwatch/watcher.py`（`print_storage`）
- Test: `tests/test_cli.py` 或 `tests/test_logic.py`（若有可注入的输出函数）

**Interfaces:**
- Produces:
  - `rw storage` 追加：`增长率`、`预计可用`、`保留期`、`状态`（ok/warning/over/unlimited）。
  - `rw storage --json`：输出与 `settings.storage_info` 同构的字典，供界面/脚本消费。
  - 内部 `storage_summary(directory, cfg, now=None) -> dict` 供 `print_storage` 与 `settings.storage_info` 共用（放在 `rules.py` 或 `settings.py`，避免两处口径不一）。

- [ ] **Step 1: 写失败测试**

```python
class StorageSummaryTest(unittest.TestCase):
    def test_summary_uses_forecast_and_config(self):
    def test_json_output_has_stable_keys(self):
```

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_cli.py`
Expected: FAIL.

- [ ] **Step 3: 实现**

- `rw storage` 保持人类可读；`--json` 时只输出 JSON（不混提示）。
- 预测逻辑复用 Task 1 的 `storage_forecast`。
- `settings.storage_info` 改为调用同一个 `storage_summary`。

- [ ] **Step 4: 运行测试通过**

Run: `python tests/test_cli.py tests/test_settings.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add roomwatch/watcher.py roomwatch/settings.py tests/test_cli.py
git commit -m "feat(storage): forecast in rw storage and json output"
```

---

### Task 8: 版本、文档、全量测试与打包

**Files:**
- Modify: `VERSION`, `CHANGELOG.md`, `docs/handoff/2026-10-19-session.md`, `AGENTS.md`（I.2 状态）
- Test: 全量套件 + 构建自检

- [ ] **Step 1: 更新版本与 CHANGELOG**

`VERSION` → `0.1.17`；CHANGELOG 顶部新增 M3 小节：
录像策略进设置、本人在场三档、存储预测与预警、编码基准结论（附实测数字）。

- [ ] **Step 2: 更新交接文档与 AGENTS**

- handoff 追加「13. 0.1.17 M3 录制/存储」：新增配置键、策略默认值、基准结论、待真机验收项。
- `AGENTS.md` I.2 标注：默认仍照录（`mute`），可用 `record.owner_at_computer: skip` 显式关闭。

- [ ] **Step 3: 跑全量测试**

Run: `C:\Users\Alec\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m roomwatch test`
Expected: 全部通过。

- [ ] **Step 4: 构建并验证**

```bash
powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1
powershell -ExecutionPolicy Bypass -File scripts/build-portable.ps1
powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1
```

Expected: exe 自检通过（8 个设置页签），产出 `dist/room-watch-0.1.17-setup.exe`。

- [ ] **Step 5: 真机验收（所有者）**

- 设置页把 `max_seconds` 改成 60，确认单段到点自动续段、总时长受 `max_total_seconds` 限制；
- 把 `owner_at_computer` 切到 `skip`，键盘活动中确认不产生新 mp4、但抓拍与推送行为符合预期；
- 切回 `mute`，确认照常录像、不推送；
- `rw storage` 与主界面显示的预测数字一致；模拟接近上限时出现预警；
- `rw bench-encode` 输出与 `rw storage` 均不打印任何密钥。

- [ ] **Step 6: 提交**

```bash
git add VERSION CHANGELOG.md docs/handoff/2026-10-19-session.md AGENTS.md
git commit -m "docs: M3 recording policy and storage forecast (0.1.17)"
```
