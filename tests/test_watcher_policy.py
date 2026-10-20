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
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from roomwatch.outbox import OutboxStore
from roomwatch.rules import OWNER_KEEP, OWNER_MUTE, OWNER_SKIP
from roomwatch.watcher import EventStore, Watcher


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


class FakeSpeaker:
    enabled = True

    def __init__(self):
        self.said = 0

    def say(self):
        self.said += 1

    def shutdown(self):
        pass


class FakeFaces:
    def __init__(self, faces):
        self.faces = faces
        self.calls = 0

    def find(self, frame, dets):
        self.calls += 1
        return self.faces


class FakeAudio:
    def __init__(self, wav):
        self.enabled = True
        self.available = True
        self.recording = True
        self.wav = wav
        self.stopped = 0

    def stop(self):
        self.stopped += 1
        self.recording = False
        return self.wav


class FakeOwnerTracker:
    def __init__(self):
        self.owner_present = False

    def observe(self, label):
        if label == "owner":
            self.owner_present = True
        return self.owner_present


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

    # ---------- 语音与事件结论 ----------

    def test_owner_cancel_does_not_speak(self):
        """侧脸进门先触发、随后认出本人：不能已经念过一句。"""
        w = self._held_watcher(label="owner", score=0.8)
        w.face_finder = FakeFaces([object()])
        try:
            Watcher.check_pending_event(
                w, object(), [object()], self.now + timedelta(seconds=2))
            self.assertEqual(w.speaker.said, 0)
        finally:
            self._close(w)

    def test_confirmed_stranger_speaks(self):
        w = self._held_watcher(label="other", score=0.10)
        w.face_finder = FakeFaces([object()])
        w.stranger_streak = 1
        try:
            Watcher.check_pending_event(
                w, object(), [object()], self.now + timedelta(seconds=2))
            self.assertEqual(w.speaker.said, 1)
        finally:
            self._close(w)

    def test_window_expiry_with_person_speaks(self):
        w = self._held_watcher(label="unknown", score=0.0)
        w.face_finder = FakeFaces([object()])
        w.event_hold_until = self.now - timedelta(seconds=1)
        try:
            Watcher.check_pending_event(w, object(), [object()], self.now)
            self.assertEqual(w.speaker.said, 1)
        finally:
            self._close(w)

    def test_snapshot_uses_video_stem_so_photo_and_video_pair(self):
        w = Watcher.__new__(Watcher)
        w.snap_dir = str(self.root / "captures")
        w.draw_boxes = False
        w.jpeg_quality = 90
        day = self.root / "captures" / "2026-10-20"
        day.mkdir(parents=True)
        w.current_video = str(day / "2026-10-20_094523.mp4")
        frame = np.zeros((8, 8, 3), np.uint8)
        with mock.patch("roomwatch.watcher.cv2.imwrite", return_value=True):
            path = Watcher.save_snapshot(w, frame, [], self.now)
        self.assertEqual(Path(path), day / "2026-10-20_094523.jpg")

    def test_voice_settings_reload_from_marker(self):
        w = Watcher.__new__(Watcher)
        w.root = str(self.root)
        w.config_path = str(self.root / "config.yaml")
        w.last_reload_check_at = None
        w.voice_cooldown = 60.0
        w.require_face = True
        w.volume_mode = "off"
        w.speaker = FakeSpeaker()
        (self.root / "config.yaml").write_text(
            "voice:\n"
            "  enabled: true\n"
            "  text: 欢迎回来\n"
            "  cooldown_seconds: 5\n"
            "  volume_mode: 'off'\n",
            encoding="utf-8")
        state = self.root / "var" / "state"
        state.mkdir(parents=True)
        marker = state / "reload.watcher"
        marker.write_text("voice", encoding="utf-8")

        created = []

        class NewSpeaker(FakeSpeaker):
            def __init__(self, cfg):
                super().__init__()
                created.append(cfg)

        with mock.patch("roomwatch.watcher.Speaker", NewSpeaker):
            Watcher.maybe_reload(w, self.now)
        self.assertFalse(marker.exists())
        self.assertAlmostEqual(w.voice_cooldown, 5.0)
        self.assertEqual(created[0]["text"], "欢迎回来")

    # ---------- 晚到本人撤销 ----------

    def _held_watcher(self, label="owner", score=0.8, recording=False):
        w = Watcher.__new__(Watcher)
        w.outbox = OutboxStore(self.root / "outbox.db")
        w.store = EventStore(str(self.root / "events.db"))
        day = self.root / "captures" / "2026-10-20"
        day.mkdir(parents=True)
        snapshot = day / "2026-10-20_100000.jpg"
        snapshot.write_bytes(b"jpeg")
        w.event_snapshot = str(snapshot)
        w.event_event_id = self.now.isoformat()
        w.event_hold_until = self.now + timedelta(seconds=15)
        w.event_resolved = False
        w.last_identity_check_at = self.now - timedelta(seconds=2)
        w.stranger_streak = 0
        w.cancelled_event_ids = set()
        w.last_event_at = self.now
        w.current_video = None
        w.recorder = SimpleNamespace(recording=recording)

        def fake_identify(frame, dets):
            w.last_identity_score = score
            return label

        w.identify = fake_identify
        w.owner_tracker = FakeOwnerTracker()
        w.is_owner_event = lambda: w.owner_tracker.owner_present
        w.last_identity_label = "other"
        w.last_identity_score = score
        w.stranger_threshold = 0.25
        w.events_today = 1
        w.speaker = FakeSpeaker()
        w.face_finder = FakeFaces([])
        w.spoke_this_event = False
        w.require_face = True
        w.face_every_n = 3
        w.frames = 1
        w.voice_cooldown = 60.0
        w.last_spoke_at = None
        w.volume_mode = "off"
        w.store.add(self.now, str(snapshot), 0.9, True)
        w.outbox.enqueue(
            channel="wecom_bot", kind="text", severity="event",
            payload={"text": "x"}, event_id=w.event_event_id,
            dedupe_key=f"{w.event_event_id}:text",
            delay_seconds=15, now=self.now)
        return w

    def _close(self, w):
        w.outbox.close()
        w.store.conn.close()

    def test_late_owner_cancels_media_outbox_and_event_row(self):
        w = self._held_watcher()
        snapshot = w.event_snapshot
        try:
            Watcher.check_pending_event(
                w, object(), [object()], self.now + timedelta(seconds=2))
            self.assertTrue(w.event_resolved)
            self.assertFalse(Path(snapshot).exists())
            self.assertEqual(w.outbox.pending_count(), 0)
            self.assertEqual(
                w.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(w.events_today, 0)
            self.assertIn(self.now.isoformat(), w.cancelled_event_ids)
        finally:
            self._close(w)

    def test_clear_stranger_releases_hold_early(self):
        w = self._held_watcher(label="other", score=0.10)
        w.stranger_streak = 1
        try:
            Watcher.check_pending_event(
                w, object(), [object()], self.now + timedelta(seconds=2))
            self.assertTrue(w.event_resolved)
            item = w.outbox.claim_next(now=self.now + timedelta(seconds=3))
            self.assertIsNotNone(item, "确认陌生人后应立即放行通知")
        finally:
            self._close(w)

    def test_expired_window_releases_notifications(self):
        w = self._held_watcher(label="unknown", score=0.0)
        w.event_hold_until = self.now - timedelta(seconds=1)
        try:
            Watcher.check_pending_event(w, object(), [object()], self.now)
            self.assertTrue(w.event_resolved)
            item = w.outbox.claim_next(now=self.now + timedelta(seconds=1))
            self.assertIsNotNone(item)
        finally:
            self._close(w)

    def test_late_owner_stops_active_recording_with_discard(self):
        w = self._held_watcher(recording=True)
        w.stop_recording = mock.Mock()
        try:
            Watcher.check_pending_event(
                w, object(), [object()], self.now + timedelta(seconds=2))
            w.stop_recording.assert_called_once()
            self.assertTrue(w.stop_recording.call_args.kwargs["discard"])
        finally:
            self._close(w)

    # ---------- 录像后端失败 ----------

    def _failure_watcher(self):
        w = self._watcher()
        w.dry_run = False
        w.recorder.recording = True
        w.recorder.last_pre_frames = 0
        w.recorder.last_error = "ffmpeg 返回 1"
        w.record_policy = SimpleNamespace(finish=lambda now: None)
        wav = self.root / "a.wav"
        wav.write_bytes(b"x")
        w.audio = FakeAudio(str(wav))
        w.event_snapshot = "2026-10-19/120000.jpg"
        w.current_video = "2026-10-19/120000.mp4"
        w.store = EventStore(str(self.root / "events.db"))
        w.store.add(self.now, w.event_snapshot, 0.9, True, video=w.current_video)
        w.outbox_path = str(self.root / "outbox.db")
        w.outbox = OutboxStore(w.outbox_path)
        w._outbox_thread = threading.get_ident()
        w._outbox_lock = threading.Lock()
        w.notify_cfg = {"channels": ["webhook"],
                        "webhook": {"url": "https://example.invalid/hook"}}
        w._video_failure_notified = False
        return w

    def test_video_backend_failure_clears_db_and_notifies(self):
        w = self._failure_watcher()
        try:
            Watcher.stop_recording(w, "测试", self.now)
            row = w.store.conn.execute(
                "SELECT video FROM events WHERE snapshot=?", (w.event_snapshot,)
            ).fetchone()
            self.assertIsNone(row[0], "坏片段的路径必须从事件库清掉")
            self.assertFalse(Path(w.audio.wav).exists(), "没有视频的 wav 也不该留着")
            self.assertEqual(w.audio.stopped, 1)
            self.assertEqual(w.outbox.pending_count(), 1)
            item = w.outbox.claim_next(now=datetime.now())
            self.assertIsNotNone(item)
            self.assertEqual(item.severity, "critical")
            self.assertIn("录像中断", item.payload.get("text", ""))
        finally:
            w.outbox.close()
            w.store.conn.close()

    def test_video_failure_notifies_only_once(self):
        w = self._failure_watcher()
        try:
            Watcher.stop_recording(w, "测试", self.now)
            w.recorder.recording = True
            Watcher.stop_recording(w, "测试", self.now)
            self.assertEqual(w.outbox.pending_count(), 1,
                             "同一进程里录像失败只提醒一次，别刷屏")
        finally:
            w.outbox.close()
            w.store.conn.close()

    def test_status_reports_video_encoder(self):
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
            recorder=SimpleNamespace(recording=False, last_encoder="h264_mf",
                                     last_error=None),
            dry_run=False,
            outbox=SimpleNamespace(stats=lambda: {}),
            notify_cfg={"channel": "none"},
            _status_write_failed=False,
            storage_forecast=None,
        )
        fake._outbox_stats = lambda: {}
        Watcher.write_status(fake)
        data = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(data["video_encoder"], "h264_mf")
        self.assertIsNone(data["video_encoder_error"])

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
