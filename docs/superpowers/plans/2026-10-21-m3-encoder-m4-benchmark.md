# M3 编码器切换与 M4 基准决策实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把事件录像默认切到 ffmpeg 硬件编码器（失败可回退），并完成 ONNX Runtime 与 cv2.dnn 的基准对比和决策记录。

**Architecture:** 新增 `roomwatch/videoenc.py` 承载编码器探测、后端实现与选择；`recorder.py` 只保留预录、队列、写线程和生命周期。watcher 在录像中途失败时丢弃坏片段、清空事件视频字段并通知。M4 增加惰性导入的 `OnnxRuntimeDetector` 与基准工具，不改变默认引擎。

**Tech Stack:** Python 3.13、OpenCV 5、ffmpeg 9（随包分发）、SQLite、unittest。可选：onnxruntime（只在基准环境安装）。

**Spec:** `docs/superpowers/specs/2026-10-21-m3-encoder-m4-benchmark-design.md`

## Global Constraints

- Windows 10/11 x64；per-user 安装；不新增随包依赖。
- `record.encoder` 默认 `auto`；auto 顺序固定 `h264_mf` → `h264_nvenc` → cv2；**libx264 不参与 auto**。
- ffmpeg 路径统一取 `record.audio.ffmpeg`（`config._prefer_bundled_ffmpeg` 已把它解析到包内 `tools/ffmpeg.exe`）。
- 任何失败都不能留下打不开的 mp4；不能静默降级。
- 版本只动修订号（本轮 0.2.2）；不擅自打 tag / 发 Release。
- 测试必须离线可跑；真实 ffmpeg 不可用时相关用例 `skipTest`。
- 每个任务结束提交一次；提交前跑该任务涉及的测试。
- 代码风格跟随现有代码：中文注释、类型注解、纯函数优先；纯逻辑优先放 `rules.py` 或新模块，不塞进 `watcher.py`。

## Review Focus

1. ffmpeg 返回 0 但文件是 0 字节或明显过短——必须按失败处理、删除并回退，不能当成功。
2. 写盘过程中磁盘满或 ffmpeg 被杀——关闭路径不能挂起，坏文件必须删除，事件要有通知。
3. watcher 停止/退出时 ffmpeg 仍在跑——`stop()` 必须在超时内收尾或强制结束，不能拖住 guardian。
4. 只有 1–2 帧的超短片段与音轨合并——必须产出可播文件或明确失败，不能生成损坏 mp4。
5. 摄像头中途改分辨率——`_fit()` 统一尺寸后 ffmpeg 输入尺寸固定，输出尺寸不变。

---

### Task 1: 编码器解析与选择

**Files:**
- Create: `roomwatch/videoenc.py`
- Test: `tests/test_videoenc.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `parse_encoders(text: str) -> set[str]`
  - `probe_encoders(ffmpeg: str, *, runner=None) -> set[str]`
  - `validate_encoder(ffmpeg: str, encoder: str, *, runner=None, tmpdir=None) -> bool`
  - `EncoderSelector(cfg: dict, *, probe=probe_encoders, validate=validate_encoder)`，
    属性 `ffmpeg: str`、`mode: str`、`requested: str`，方法 `choose() -> str | None`
    （返回可用的 ffmpeg 编码器名；`None` 表示用 cv2）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_videoenc.py` 写：
- `TestParseEncoders.test_extracts_names_from_ffmpeg_output`：给一段包含
  ` h264_mf `、` h264_nvenc `、` libx264 ` 与一行 `------` 的文本，断言集合包含
  三个名字且不含 `------`。
- `TestEncoderSelector.test_auto_prefers_mf_then_nvenc`：注入 `probe` 返回
  `{"h264_mf","h264_nvenc"}`、`validate` 恒真，断言 `choose() == "h264_mf"`。
- `test_auto_skips_mf_when_validation_fails`：`validate` 对 `h264_mf` 返回 False、
  对 `h264_nvenc` 返回 True，断言选 `h264_nvenc`。
- `test_auto_returns_none_when_no_hardware_encoder`：probe 只有 `{"libx264"}`，
  断言 `choose() is None`（libx264 不进 auto）。
- `test_explicit_encoder_is_tried_first`：cfg `ffmpeg_encoder="libx264"`，probe 有它、
  validate 真，断言选 `libx264`。
- `test_explicit_encoder_falls_back_to_auto_order`：显式 `libx264` 验证失败，
  probe 有 `h264_mf`，断言选 `h264_mf`。
- `test_encoder_cv2_disables_ffmpeg`：`encoder="cv2"`，断言 `choose() is None`
  且 probe 未被调用。
- `test_missing_ffmpeg_returns_none`：probe 抛 `FileNotFoundError`，断言 `None`。
- `test_choice_is_cached`：连续两次 `choose()` 只触发一次 probe/validate。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/test_videoenc.py -v`
Expected: `ModuleNotFoundError` / `ImportError: cannot import name 'EncoderSelector'`

- [ ] **Step 3: 实现 `roomwatch/videoenc.py`**

- `parse_encoders` 按行取第二个空白分隔字段，跳过以 `-` 开头的分隔行。
- `probe_encoders` 运行 `[ffmpeg, "-hide_banner", "-encoders"]`，`text=True,
  encoding="utf-8", errors="replace"`，加 `CREATE_NO_WINDOW`（Windows），
  返回解析结果；`runner` 可注入。
- `validate_encoder` 用 3 帧 64×64 纯色画面，命令固定为
  `-f rawvideo -pix_fmt bgr24 -s 64x64 -r 10 -i - -frames:v 3 -c:v <enc>
  -pix_fmt yuv420p -y <tmp>`；返回 `returncode == 0` 且文件存在且大小 > 0；
  异常/超时返回 False。
- `EncoderSelector.choose`：`encoder=="cv2"` 直接 None；候选顺序为
  显式 `ffmpeg_encoder`（若不是 auto）→ `h264_mf` → `h264_nvenc`；
  对每个候选先确认在 `probe()` 结果里，再 `validate()`；第一个通过即缓存返回。

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/test_videoenc.py -v`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add roomwatch/videoenc.py tests/test_videoenc.py
git commit -m "feat: ffmpeg 编码器探测与选择"
```

---

### Task 2: FfmpegWriter 进程封装

**Files:**
- Modify: `roomwatch/videoenc.py`
- Test: `tests/test_videoenc.py`

**Interfaces:**
- Consumes: Task 1 的模块。
- Produces:
  - `FfmpegWriter(ffmpeg: str, encoder: str, path: str, size: tuple[int,int],
    fps: float, *, timeout: float = 8.0, spawn=None)`
  - 属性 `description: str`（返回编码器名，如 `h264_mf`）、`error: str | None`、
    `path: str`、`written: int`
  - `open() -> bool`、`write(frame) -> None`、`close() -> bool`
    （True = 进程正常退出且文件非空）。

- [ ] **Step 1: 写失败测试（注入 FakeProc）**

在 `tests/test_videoenc.py` 增加 `FakeProc`（stdin 记录写入、wait 返回码可控、
poll/kill 记录调用）与测试：
- `test_command_contains_encoder_size_fps_path`：断言 spawn 收到的命令包含
  `h264_mf`、`640x360`、`-r` 后跟 fps、输出路径。
- `test_write_sends_raw_frame_bytes`：写一帧 `numpy` 画面，断言 stdin 收到的
  字节数等于 `frame.nbytes`。
- `test_close_ok_returns_true`：wait 返回 0、文件存在且非空，断言 True。
- `test_close_on_nonzero_rc_deletes_file`：wait 返回 1，断言 close 为 False、
  文件被删除、`error` 非空。
- `test_close_timeout_kills_process`：wait 抛 `TimeoutExpired`，断言 kill 被调用、
  close 返回 False。
- `test_zero_byte_file_is_failure`：rc=0 但文件为空，断言 False 且删除。
- 真实 ffmpeg 用例 `test_real_ffmpeg_roundtrip`：找 `record.audio.ffmpeg` →
  `shutil.which("ffmpeg")`，都找不到则 `skipTest`；编 10 帧后用 cv2 读回，
  断言帧数 > 0。
- 真实 ffmpeg 用例 `test_single_frame_segment_is_valid`：只写 1 帧，`close()`
  返回 True 且 cv2 能读出至少 1 帧（Review Focus 4：超短片段）。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_videoenc.py FfmpegWriterTest -v`
Expected: FAIL/ERROR，`FfmpegWriter` 不存在。

- [ ] **Step 3: 实现 `FfmpegWriter`**

命令固定为：
`[ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
"-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
"-c:v", encoder, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
"-y", path]`。
- `stdout=DEVNULL`，`stdin=PIPE`，`stderr=PIPE`，Windows 加 `CREATE_NO_WINDOW`。
- 起一个 daemon 线程读 stderr，保留最后 20 行。
- `write` 写 `frame.tobytes()`；`BrokenPipeError` 转成 `RuntimeError` 并设置
  `error`。
- `close`：关 stdin → `wait(timeout)`；超时 `kill()` + `wait(2)`；rc 非 0、
  文件不存在或大小为 0 都算失败，失败时删除文件并返回 False；最后 join
  stderr 线程（短超时）。

- [ ] **Step 4: 运行确认通过**

Run: `python tests/test_videoenc.py -v`
Expected: OK（真实 ffmpeg 用例本机应执行，不应跳过）

- [ ] **Step 5: 提交**

```bash
git add roomwatch/videoenc.py tests/test_videoenc.py
git commit -m "feat: ffmpeg 写帧后端"
```

---

### Task 3: Recorder 接入后端与失败返回

**Files:**
- Modify: `roomwatch/recorder.py`（`_open_writer`、`CODEC_CHAIN`、`Recorder.start/stop`）
- Modify: `roomwatch/videoenc.py`（新增 `Cv2Writer`、`WriterFactory`）
- Test: `tests/test_recorder.py`、`tests/test_videoenc.py`

**Interfaces:**
- Produces:
  - `Cv2Writer(path: str, size: tuple[int,int], fps: float, codecs: list[str])`，
    方法 `open() -> bool`、`write(frame)`、`close() -> bool`、属性
    `description`、`path`。
  - `WriterFactory(cfg: dict, *, selector=None, cv2_factory=Cv2Writer)`，
    方法 `open(path, size, fps) -> Writer | None`、`note_segment_failed()`，
    属性 `disabled_reason: str | None`。
  - `Recorder(cfg, fps, *, factory=None)`，新增属性 `last_encoder: str | None`；
    `stop()` 在写出失败时返回 `None`。写线程里 `writer.write()` 抛异常时
    置 `_write_failed=True`；`stop()` 即使 `close()` 返回 True，也要删除文件并
    返回 `None`（模拟磁盘满/写失败，Review Focus 2）。

- [ ] **Step 1: 更新现有测试并写新失败测试**

- `tests/test_recorder.py` 的 `_rec()` 默认加入 `"encoder": "cv2"`，保证既有用例
  仍走 cv2、不依赖 ffmpeg。
- 新增 `TestRecorderBackendFailure`：
  - 注入 fake factory，其 `open()` 返回一个 `write()` 抛异常、`close()` 返回 False
    的 writer；`start()` 后 `stop()` 必须返回 `None`，文件被删除，`recording` 为
    False，`last_error` 非空。
  - `test_last_encoder_is_reported`：fake writer `description="h264_mf"`，开始录制后
    `rec.last_encoder == "h264_mf"`。
  - `test_write_error_discards_segment`：fake writer 的 `write()` 抛 `OSError`
    （模拟磁盘满），即使 `close()` 返回 True，`stop()` 也必须返回 None、
    删除文件。
  - `test_mismatched_frame_size_with_ffmpeg_backend`（真实 ffmpeg）：以 640×360
    开录，喂一帧 1280×720，收尾后读回尺寸仍为 640×360（Review Focus 5）。
- 在 `tests/test_videoenc.py` 增加 `TestWriterFactory`：
  - 没有可用 ffmpeg 编码器（selector 返回 None）→ 返回 Cv2Writer；
  - selector 返回 `h264_mf` 且 ffmpeg writer 打开成功 → 返回 FfmpegWriter；
  - ffmpeg writer 打开失败 → 回退 Cv2Writer，并记录 `disabled_reason`；
  - 连续两次失败后 `note_segment_failed()` 达阈值，第三次不再尝试 ffmpeg。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_recorder.py TestRecorderBackendFailure -v`
Expected: FAIL（`Recorder` 不接受 `factory` / `last_encoder` 不存在）。

- [ ] **Step 3: 实现**

- 把 `_open_writer` 与 `CODEC_CHAIN` 从 `recorder.py` 移到 `videoenc.py`，
  `Cv2Writer` 复用原逻辑（尝试 `record.codec` + 内置链）。
- `WriterFactory.open`：按 `record.encoder`（cv2/ffmpeg/auto）与
  `EncoderSelector.choose()` 选后端；ffmpeg 打开失败时调用 `note_segment_failed()`
  并回退 cv2；连续 2 次失败后 `disabled_reason="ffmpeg 连续失败"`，本进程只用 cv2。
- `Recorder.__init__`：`self._factory = factory or WriterFactory(cfg)`；
  `Recorder.start` 用 `self._factory.open(...)`，成功后设置
  `self.last_encoder = writer.description`；`stop` 用 `writer.close()`，
  返回 False 时删除文件、设 `last_error`、返回 None。
- 保留队列、写线程、预录、`_fit`、计数器语义不变。

- [ ] **Step 4: 运行全部录像测试**

Run: `python tests/test_recorder.py -v` 与 `python tests/test_videoenc.py -v`
Expected: 两个文件都 OK。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/recorder.py roomwatch/videoenc.py tests/test_recorder.py tests/test_videoenc.py
git commit -m "feat: 录像器接入 ffmpeg 后端并支持失败返回"
```

---

### Task 4: watcher 失败处理、事件库与状态

**Files:**
- Modify: `roomwatch/watcher.py`（`EventStore.clear_video_by_snapshot`、
  `Watcher.stop_recording`、`Watcher.write_status`）
- Test: `tests/test_recorder.py`（EventStore）、`tests/test_watcher_policy.py`

**Interfaces:**
- Consumes: Task 3 的 `Recorder.stop() -> str | None`、`Recorder.last_encoder`。
- Produces:
  - `EventStore.clear_video_by_snapshot(snapshot: str) -> int`
  - `Watcher._notify_video_failure(snapshot: str, reason: str) -> None`

- [ ] **Step 1: 写失败测试**

- `TestEventStore.clear_video`: 插入一条带 `video` 的事件，调用新方法后该行
  `video` 为 NULL，其它列不变。
- 在 `tests/test_watcher_policy.py` 的现有 Watcher 构造工具上新增：
  - `test_video_backend_failure_clears_db_and_notifies`：让 `recorder.stop()`
    返回 None（注入 fake recorder 或 patch），断言事件库该行 `video` 为空、
    `audio.merge_async` 未被调用、outbox 收到一条 `severity="critical"` 的通知、
    返回的 status 里 `video_encoder_error` 非空。
  - `test_video_failure_notifies_only_once`：连续两次失败，outbox 里 critical
    只有一条。
  - `test_status_reports_video_encoder`：fake recorder `last_encoder="h264_mf"`，
    `write_status` 后 JSON 的 `video_encoder == "h264_mf"`。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_recorder.py TestEventStore -v` 与
`python tests/test_watcher_policy.py -v`
Expected: 新用例失败（方法/字段不存在）。

- [ ] **Step 3: 实现**

- `EventStore.clear_video_by_snapshot`：`UPDATE events SET video=NULL WHERE snapshot=?`，
  返回 `rowcount`。
- `stop_recording`：`video = self.recorder.stop(reason)` 后：
  - `discard` 分支保持现状；
  - `video is None` 且不是 discard：停止并删除 wav，调用
    `self.store.clear_video_by_snapshot(self.event_snapshot)`（若存在），
    调用 `_notify_video_failure`，然后 return；
  - 正常分支不变。
- `_notify_video_failure`：`self.dry_run` 时不发；进程内只发一次
  （实例标志 `self._video_failure_notified`）；用
  `build_items(..., event_id=f"video-failed:{snapshot}", text="录像中断，已回退",
  severity="critical")` 入 outbox；同时 `logutil.event(log, "video_backend_failed", ...)`。
- `write_status` 增加 `"video_encoder": getattr(self.recorder, "last_encoder", None)`
  与 `"video_encoder_error": self.recorder.last_error`。

- [ ] **Step 4: 运行相关测试**

Run: `python tests/test_watcher_policy.py -v` 与 `python tests/test_recorder.py -v`
Expected: OK。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/watcher.py tests/test_recorder.py tests/test_watcher_policy.py
git commit -m "feat: 录像后端失败时清空事件视频并通知"
```

---

### Task 5: ONNX Runtime 后端与 bench-detect 对比

**Files:**
- Modify: `roomwatch/onnx_detector.py`（`OnnxRuntimeDetector`、`build_detector`）
- Modify: `roomwatch/benchdetect.py`（`--engine`、p50/p95、一致性对比）
- Test: `tests/test_onnx_detector.py`、`tests/test_benchdetect.py`

**Interfaces:**
- Produces:
  - `OnnxRuntimeDetector(model_path, imgsz=640, conf=0.55, iou=0.45, threads=2)`，
    `name="onnxruntime"`，`detect(frame) -> list[Detection]`；未安装 onnxruntime
    时构造抛 `RuntimeError("onnxruntime 未安装")`，导入模块不受影响。
  - `build_detector` 支持 `engine="onnxruntime"`；`auto` 行为不变。
  - `percentile(values: list[float], p: float) -> float`
  - `detections_match(a, b, *, conf_tol=0.02, iou_tol=0.95) -> bool`

- [ ] **Step 1: 写失败测试**

- `tests/test_onnx_detector.py`：
  - `test_module_imports_without_onnxruntime`：`import roomwatch.onnx_detector` 正常；
    若本机没有 onnxruntime，构造 `OnnxRuntimeDetector` 抛 `RuntimeError`（用
    `importlib.util.find_spec` 判断是否跳过）。
- 新建 `tests/test_benchdetect.py`：
  - `test_percentile_p50_p95`：`[1..100]` 的 p50≈50.5、p95≈95.05（允许 1 位误差）。
  - `test_detections_match_identical`、`test_detections_match_tolerates_conf_drift`、
    `test_detections_match_rejects_missing_box`、`test_detections_match_rejects_low_iou`。
  - `test_engine_flag_unknown_value`：argparse `--engine nope` 退出码 2。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_benchdetect.py -v`
Expected: ImportError（`percentile` 等不存在）。

- [ ] **Step 3: 实现**

- `OnnxRuntimeDetector.detect`：与 `OnnxDetector.detect` 相同的前后处理
  （`letterbox` → `blobFromImage` → `decode` → `nms`），推理改为
  `session.run(None, {input_name: blob})`；`sess_options.intra_op_num_threads=threads`。
- `build_detector` 增加 `engine == "onnxruntime"` 分支；`auto`/`onnx`/`torch` 与
  未知值的行为保持不变（未知值仍按 auto 处理）。
- `benchdetect`：`--engine auto|cv2|onnxruntime|torch`（默认 auto；`cv2` 在内部映射为
  现有的 `engine="onnx"`，不改 `detector.engine` 的既有取值），
  `--compare cv2,onnxruntime` 可选；`bench_detector` 记录每帧耗时并计算 p50/p95；
  `--json` 输出新增 `p50_ms`、`p95_ms`。
- 一致性对比：按中心点排序后逐框比较类别、置信度与 IoU。

- [ ] **Step 4: 运行确认通过**

Run: `python tests/test_benchdetect.py -v` 与 `python tests/test_onnx_detector.py -v`
Expected: OK（无 onnxruntime 的用例跳过）。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/onnx_detector.py roomwatch/benchdetect.py tests/test_onnx_detector.py tests/test_benchdetect.py
git commit -m "feat: 可选 ONNX Runtime 后端与推理基准对比"
```

---

### Task 6: `rw bench` 健康采样

**Files:**
- Create: `roomwatch/bench.py`
- Modify: `roomwatch/cli.py`（`COMMANDS["bench"]` 与分派）
- Test: `tests/test_bench.py`

**Interfaces:**
- Produces:
  - `summarize_samples(samples: list[dict]) -> dict`，字段：
    `count`、`cpu_mean`、`cpu_peak`、`rss_mean_mb`、`rss_peak_mb`、
    `stale_samples`、`frames_latest`。
  - `sample_health(path: str, seconds: float, interval: float, *,
    reader=read_json, clock=time.monotonic, sleep=time.sleep) -> list[dict]`
  - `main(argv=None) -> int`；`rw bench --seconds 60 --interval 1 --json`。

- [ ] **Step 1: 写失败测试**

`tests/test_bench.py`：
- `test_summarize_mean_and_peak`：三份样本，断言均值/峰值。
- `test_summarize_handles_empty`：空列表返回零值、不抛。
- `test_summarize_counts_stale`：`stale=true` 的样本计数正确。
- `test_sample_health_uses_injected_clock`：注入 clock/sleep/reader，断言采样次数
  与间隔，不真睡。
- `test_bench_command_registered`：`roomwatch.cli.COMMANDS` 里有 `bench`。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_bench.py -v`
Expected: ModuleNotFoundError。

- [ ] **Step 3: 实现**

- `read_json` 读取 `var/state/health.json`，失败返回 `{}`（采样继续）。
- `sample_health` 按 `interval` 采样直到 `seconds`；异常样本跳过。
- `main` 参数 `--seconds`（默认 60）、`--interval`（默认 1）、`--json`。
- `cli.py` 分派：`if name == "bench": from . import bench; return bench.main(rest)`。

- [ ] **Step 4: 运行确认通过**

Run: `python tests/test_bench.py -v`
Expected: OK。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/bench.py roomwatch/cli.py tests/test_bench.py
git commit -m "feat: rw bench 采样运行中的健康数据"
```

---

### Task 7: 基准报告与版本收尾

**Files:**
- Modify: `roomwatch/benchcodec.py`（加入 `libx264` 对比项）
- Create: `docs/bench-2026-10-21-m3-m4.md`
- Modify: `VERSION`（0.2.2）、`CHANGELOG.md`、`AGENTS.md`、`docs/handoff/2026-10-20-next-session.md`

- [ ] **Step 1: 写失败测试（libx264 候选）**

`tests/test_benchcodec.py`（若不存在则新建）：
- `test_candidates_include_libx264`：`benchcodec.ffmpeg_candidates({"h264_mf","h264_nvenc"})`
  包含 `libx264`，且顺序为 `h264_nvenc` → `h264_qsv` → `h264_amf` → `h264_mf`
  → `libx264`（只保留 available 里的项）。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/test_benchcodec.py -v`
Expected: FAIL（函数不存在或列表不含 libx264）。

- [ ] **Step 3: 实现并跑基准**

- 在 `benchcodec.py` 加入 `ffmpeg_candidates`，`run_all` 改用它。
- 跑：
  `python -m roomwatch bench-encode --frames 120 --json`（记下 cv2 与各 ffmpeg 编码器）。
- 建基准环境（只在构建机）：
  `python -m venv --system-site-packages build/bench-venv`，
  `build\bench-venv\Scripts\pip install onnxruntime`。
- 跑：
  `build\bench-venv\Scripts\python.exe -m roomwatch bench-detect --engine cv2 --frames 100 --json`
  与 `--engine onnxruntime`，同尺寸、同线程数；各跑 3 次取中位数。
- 跑 `python -m roomwatch bench --seconds 60 --json` 采样运行中的监控。
- 写 `docs/bench-2026-10-21-m3-m4.md`：机器信息、命令、原始数字、
  M3 体积对比、M4 是否达到 ≥20% 门槛的结论。
- 更新 `VERSION` 为 `0.2.2`，CHANGELOG 顶部加 `[0.2.2]` 小节
  （编码器切换、失败处理、M4 报告与结论）；AGENTS J 与 handoff 追加本轮结果。

- [ ] **Step 4: 运行确认通过**

Run: `python tests/test_benchcodec.py -v` 与 `python -m roomwatch version`
Expected: 测试 OK；版本打印 `0.2.2`。

- [ ] **Step 5: 提交**

```bash
git add roomwatch/benchcodec.py tests/test_benchcodec.py docs/bench-2026-10-21-m3-m4.md VERSION CHANGELOG.md AGENTS.md docs/handoff/2026-10-20-next-session.md
git commit -m "feat: 0.2.2 编码器与推理基准报告"
```

---

### Task 8: 全量测试、构建与真机验证

**Files:**
- 无代码改动；产出 `dist\room-watch-0.2.2-setup.exe` 与验证记录。

- [ ] **Step 1: 全量测试**

Run: `python -m roomwatch test`
Expected: 汇总「全部通过」。

- [ ] **Step 2: 构建**

依次运行（PowerShell）：
`scripts\build-exe.ps1` → `scripts\build-portable.ps1 -SkipDeps`（若 `dist\room-watch-0.2.2\`
已存在且 runtime 完整）或完整 `build-portable.ps1` → `scripts\build-installer.ps1`。
Expected: `dist\room-watch-0.2.2-setup.exe` 生成，`room-watch.exe --selftest` 通过。

- [ ] **Step 3: 安装并验证默认编码器**

用终端面板（真实用户会话）静默安装；`rw doctor`、卸载项、`rw version` 均为
0.2.2。触发一次事件后检查 `var/state/status.json`：
`video_encoder == "h264_mf"`、`video_encoder_error` 为空；片段能在相册播放、
有声音、开头有预录。

- [ ] **Step 4: 验证回退路径**

- 配置 `record.ffmpeg_encoder: h264_nvenc` 录一段，确认 `video_encoder` 为
  `h264_nvenc` 且可播；
- 配置 `libx264` 再录一段，确认可播；
- 把 `record.audio.ffmpeg` 指向不存在的文件，录一段，确认回退 avc1、
  `video_encoder_error` 有原因、片段可播；
- 恢复配置，重启 watcher/guardian。

- [ ] **Step 5: 体积对比与记录**

比较同一场景下 0.2.1（avc1）与 0.2.2（h264_mf）的每分钟体积，确认下降 ≥40%；
把结果补进 `docs/bench-2026-10-21-m3-m4.md` 和 handoff。

- [ ] **Step 6: 提交文档与验证记录**

```bash
git add docs/bench-2026-10-21-m3-m4.md docs/handoff/2026-10-20-next-session.md AGENTS.md
git commit -m "docs: 0.2.2 真机编码器验证"
```
