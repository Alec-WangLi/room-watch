"""watcher 的录像策略与存储状态测试（离线，不碰摄像头/编码器）。

    python tests/test_watcher_policy.py

为什么用 `Watcher.__new__`：这里只验证「策略在闸门上有没有被执行」，
不需要摄像头、模型和真实编码器；构造完整 Watcher 反而会把测试拖进硬件依赖。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from roomwatch.outbox import OutboxStore
from roomwatch.rules import OWNER_KEEP, OWNER_MUTE, OWNER_SKIP
from roomwatch.watcher import Watcher


class FakeRecorder:
    def __init__(self, recording: bool = False):
        self.enabled = True
        self.recording = recording
        self.started = []
        self.observed = 0
        self.stopped = []

    def start(self, path, when):
        self.started.append(path)
        self.recording = True
        return path

    def observe(self, frame, when, has_person=True):
        self.observed += 1

    def stop(self, reason=""):
        self.stopped.append(reason)
        self.recording = False
        return None


class WatcherPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 19, 12, 0, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _watcher(self, policy=OWNER_MUTE, user_active=False, recording=False):
        w = Watcher.__new__(Watcher)
        w.owner_policy = policy
        w.user_is_active = lambda: user_active
        w.recorder = FakeRecorder(recording=recording)
        w.snap_dir = str(self.root / "captures")
        w.audio = SimpleNamespace(enabled=False, available=False)
        w.record_policy = SimpleNamespace(begin=lambda *a, **k: None)
        w.current_video = None
        return w

    # ---------- 开录闸门 ----------

    def test_skip_policy_does_not_start_recording_while_user_active(self):
        w = self._watcher(OWNER_SKIP, user_active=True)
        Watcher.start_recording(w, [], self.now)
        self.assertEqual(w.recorder.started, [])
        self.assertFalse(w.recorder.recording)

    def test_mute_policy_starts_recording_while_user_active(self):
        w = self._watcher(OWNER_MUTE, user_active=True)
        Watcher.start_recording(w, [], self.now)
        self.assertEqual(len(w.recorder.started), 1)

    def test_keep_policy_starts_recording_while_user_active(self):
        w = self._watcher(OWNER_KEEP, user_active=True)
        Watcher.start_recording(w, [], self.now)
        self.assertEqual(len(w.recorder.started), 1)

    def test_skip_policy_records_when_user_is_not_active(self):
        w = self._watcher(OWNER_SKIP, user_active=False)
        Watcher.start_recording(w, [], self.now)
        self.assertEqual(len(w.recorder.started), 1)

    # ---------- 推送闸门 ----------

    def test_notify_gate_follows_policy(self):
        cases = [
            (OWNER_KEEP, True, True),
            (OWNER_MUTE, True, False),
            (OWNER_SKIP, True, False),
            (OWNER_SKIP, False, True),
        ]
        for policy, active, expected in cases:
            w = self._watcher(policy, user_active=active)
            w.dry_run = False
            with self.subTest(policy=policy, active=active):
                self.assertEqual(Watcher.notify_allowed(w), expected)

    def test_dry_run_still_suppresses_everything(self):
        w = self._watcher(OWNER_KEEP, user_active=False)
        w.dry_run = True
        self.assertFalse(Watcher.notify_allowed(w))

    # ---------- 不打断进行中的录像 ----------

    def test_policy_does_not_stop_in_flight_recording(self):
        w = self._watcher(OWNER_SKIP, user_active=True, recording=True)
        w.decorate_video_frame = lambda frame, dets, now: frame
        w.record_policy = SimpleNamespace(
            note=lambda now, has_person: None,
            check=lambda now: "",
        )
        frame = np.zeros((16, 16, 3), np.uint8)
        Watcher.pump_recording(w, frame, [], self.now)
        self.assertEqual(w.recorder.stopped, [])
        self.assertEqual(w.recorder.observed, 1)

    # ---------- 配置错误分类 ----------

    def test_bad_numeric_config_exits_with_config_code(self):
        """imgsz 写成字符串应报配置错误（2），不能落进「未处理异常」（5）。"""
        from roomwatch import health

        with self.assertRaises(SystemExit) as ctx:
            Watcher({"detector": {"imgsz": "abc"}})
        self.assertEqual(ctx.exception.code, health.EXIT_CONFIG)

    # ---------- 跨线程入队 ----------

    def test_enqueue_from_merge_thread_uses_its_own_connection(self):
        """录像合完音轨后的回调在 audio-merge 线程里入队，不能复用主线程连接。"""
        w = Watcher.__new__(Watcher)
        w.outbox_path = str(self.root / "outbox.db")
        w.outbox = OutboxStore(w.outbox_path)
        w._outbox_thread = threading.get_ident()
        w._outbox_lock = threading.Lock()
        seen = {}

        def work():
            seen["n"] = Watcher.enqueue_notification(w, [{
                "channel": "wecom_bot",
                "kind": "video",
                "severity": "event",
                "payload": {"video_path": "x.mp4"},
                "event_id": "e1",
                "dedupe_key": "e1:wecom_bot:video",
            }])

        try:
            worker = threading.Thread(target=work)
            worker.start()
            worker.join(timeout=10)
            self.assertEqual(seen.get("n"), 1)
            self.assertEqual(w.outbox.pending_count(), 1)
        finally:
            w.outbox.close()

    # ---------- 状态文件 ----------

    def test_status_includes_storage_forecast(self):
        status_path = self.root / "status.json"
        fake = SimpleNamespace(
            status_path=str(status_path),
            started_at=self.now,
            last_frame_at=self.now,
            stale_seconds=120,
            camera_spec=SimpleNamespace(label="cam"),
            detector=SimpleNamespace(name="onnx"),
            gate=SimpleNamespace(stats=lambda: {}),
            frames=10,
            events_today=0,
            recorder=SimpleNamespace(recording=False),
            dry_run=False,
            outbox=SimpleNamespace(stats=lambda: {}),
            notify_cfg={"channel": "none"},
            _status_write_failed=False,
            storage_forecast={
                "status": "warning",
                "used_bytes": 8 * 1024 ** 3,
                "max_bytes": 10 * 1024 ** 3,
                "days_to_cap": 2.4,
                "projected_days": 2.4,
            },
        )
        fake._outbox_stats = lambda: {}
        Watcher.write_status(fake)
        data = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(data["storage_status"], "warning")
        self.assertAlmostEqual(data["storage_used_gb"], 8.0, places=2)
        self.assertAlmostEqual(data["storage_max_gb"], 10.0, places=2)
        self.assertAlmostEqual(data["storage_days_left"], 2.4, places=1)

    def test_status_without_forecast_has_null_storage(self):
        status_path = self.root / "status.json"
        fake = SimpleNamespace(
            status_path=str(status_path),
            started_at=self.now,
            last_frame_at=self.now,
            stale_seconds=120,
            camera_spec=SimpleNamespace(label="cam"),
            detector=SimpleNamespace(name="onnx"),
            gate=SimpleNamespace(stats=lambda: {}),
            frames=10,
            events_today=0,
            recorder=SimpleNamespace(recording=False),
            dry_run=False,
            outbox=SimpleNamespace(stats=lambda: {}),
            notify_cfg={"channel": "none"},
            _status_write_failed=False,
            storage_forecast=None,
        )
        fake._outbox_stats = lambda: {}
        Watcher.write_status(fake)
        data = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertIsNone(data["storage_status"])
        self.assertIsNone(data["storage_days_left"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
