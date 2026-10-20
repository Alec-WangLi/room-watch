"""watcher 接入 outbox 的集成测试（离线，不碰摄像头/网络）。

    python tests/test_outbox_integration.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from roomwatch import outbox as O
from roomwatch.watcher import EventStore, Watcher


class WatcherOutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.w = Watcher.__new__(Watcher)       # 不初始化摄像头/模型
        self.w.outbox = O.OutboxStore(self.root / "outbox.db")
        self.w.notify_cfg = {"channels": ["wecom_bot"],
                             "wecom_bot": {"send_video": True}}
        self.w.dry_run = False
        self.w.events_today = 0
        self.w.last_identity_label = "other"
        self.w.last_identity_score = 0.0
        self.w.stranger_threshold = 0.25
        self.w.cancelled_event_ids = set()
        self.w.current_video = None
        self.w.last_event_at = None
        self.w.heartbeat_minutes = 30
        self.w.last_heartbeat_at = None
        self.w.started_at = datetime.now()
        self.w.last_frame_at = datetime.now()
        self.w.store = EventStore(str(self.root / "events.db"))
        self.image = self.root / "shot.jpg"
        self.image.write_bytes(b"jpeg")
        self.w.save_snapshot = lambda frame, dets, when: str(self.image)
        self.w.notify_allowed = lambda: True

    def tearDown(self):
        self.w.outbox.close()
        self.w.store.conn.close()
        self.tmp.cleanup()

    def _kinds(self):
        return [row["kind"] for row in self.w.outbox.conn.execute(
            "SELECT kind FROM outbox ORDER BY id")]

    def test_fire_event_enqueues_text_and_image(self):
        self.w.fire_event(None, [SimpleNamespace(conf=0.9)], datetime.now())
        self.assertEqual(self._kinds(), ["text", "image"])
        self.assertEqual(self.w.outbox.stats()["pending"], 2)
        row = self.w.store.conn.execute(
            "SELECT identity, notified FROM events").fetchone()
        self.assertEqual(row[0], "other")
        self.assertEqual(row[1], 1)

    def test_fire_event_holds_notifications_for_grace(self):
        self.w.fire_event(None, [SimpleNamespace(conf=0.9)], datetime.now())
        self.assertFalse(self.w.event_resolved)
        self.assertIsNotNone(self.w.event_hold_until)
        self.assertEqual(self.w.event_event_id, self.w.last_event_at.isoformat())
        self.assertIsNone(self.w.outbox.claim_next(),
                          "等待窗口内通知不应被 worker 领取")

    def test_user_active_suppresses_enqueue(self):
        self.w.notify_allowed = lambda: False
        self.w.fire_event(None, [SimpleNamespace(conf=0.9)], datetime.now())
        self.assertEqual(self.w.outbox.pending_count(), 0)
        row = self.w.store.conn.execute("SELECT notified FROM events").fetchone()
        self.assertEqual(row[0], 0)

    def test_push_video_enqueues_only_video(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"v" * 100)
        self.w.last_event_at = datetime.now()
        self.w.push_video(str(video))
        self.assertEqual(self._kinds(), ["video"])

    def test_maybe_heartbeat_enqueues_digest(self):
        self.w.maybe_heartbeat(datetime.now())
        row = self.w.outbox.conn.execute(
            "SELECT kind, severity FROM outbox").fetchone()
        self.assertEqual(row["kind"], "text")
        self.assertEqual(row["severity"], "digest")

    def test_status_reports_pending_outbox(self):
        self.w.fire_event(None, [SimpleNamespace(conf=0.9)], datetime.now())
        fake = SimpleNamespace(
            status_path=str(self.root / "status.json"),
            started_at=self.w.started_at,
            last_frame_at=self.w.last_frame_at,
            stale_seconds=120,
            camera_spec=SimpleNamespace(label="cam"),
            detector=SimpleNamespace(name="onnx"),
            gate=SimpleNamespace(stats=lambda: {}),
            frames=1,
            events_today=0,
            recorder=SimpleNamespace(recording=False),
            dry_run=False,
            outbox=self.w.outbox,
            notify_cfg=self.w.notify_cfg,
            _status_write_failed=False,
        )
        fake._outbox_stats = lambda: self.w.outbox.stats()
        Watcher.write_status(fake)
        data = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(data["pending_outbox"], 2)
        self.assertIn("last_push_success_at", data)


class EventStoreDeleteTest(unittest.TestCase):
    def test_delete_by_snapshot_removes_only_that_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "events.db"))
            try:
                now = datetime.now()
                store.add(now, "a.jpg", 0.9, True)
                store.add(now, "b.jpg", 0.8, True)
                removed = store.delete_by_snapshot("a.jpg")
                rows = store.conn.execute("SELECT snapshot FROM events").fetchall()
            finally:
                store.conn.close()
        self.assertEqual(removed, 1)
        self.assertEqual([row[0] for row in rows], ["b.jpg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
