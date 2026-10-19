"""outbox 存储与状态机单测。

    python tests/test_outbox.py

重点不是 SQL 本身，而是三条产品承诺：
1. 同一事件不会因为重试而重复发送；
2. 进程在「发送中」被杀，重启后还能继续；
3. 断网恢复后积压文字会送达，且视频不会把文字挤出队列。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from roomwatch import outbox as O


class OutboxFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "outbox.db"
        self.media = self.root / "media"
        self.media.mkdir()
        self.store = O.OutboxStore(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enqueue(self, **kwargs):
        defaults = dict(channel="wecom_bot", kind="text", severity="event",
                        payload={"text": "有人进入"}, event_id="e1",
                        dedupe_key="e1:wecom_bot:text")
        defaults.update(kwargs)
        return self.store.enqueue(**defaults)


class EnqueueClaimTest(OutboxFixture):
    def test_enqueue_claim_and_deliver(self):
        item_id = self.enqueue()
        item = self.store.claim_next()
        self.assertEqual(item.id, item_id)
        self.assertEqual(item.status, O.STATUS_SENDING)
        self.assertEqual(item.payload["text"], "有人进入")
        self.store.mark_delivered(item.id)
        stats = self.store.stats()
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertIsNotNone(self.store.last_success_at())

    def test_duplicate_dedupe_key_is_ignored(self):
        self.assertIsNotNone(self.enqueue())
        self.assertIsNone(self.enqueue())
        self.assertEqual(self.store.pending_count(), 1)

    def test_rows_survive_reopen(self):
        self.enqueue()
        self.store.close()
        self.store = O.OutboxStore(self.db)
        item = self.store.claim_next()
        self.assertIsNotNone(item)


class PriorityTest(OutboxFixture):
    def test_text_before_video(self):
        self.enqueue(kind="video", dedupe_key="e1:wecom_bot:video",
                     payload={"video_path": "a.mp4"})
        self.enqueue(kind="text", severity="critical", dedupe_key="e2:wecom_bot:text")
        item = self.store.claim_next()
        self.assertEqual(item.kind, "text")
        self.assertEqual(item.severity, "critical")

    def test_kinds_filter(self):
        self.enqueue(kind="video", dedupe_key="e1:wecom_bot:video",
                     payload={"video_path": "a.mp4"})
        item = self.store.claim_next(kinds=("text", "image"))
        self.assertIsNone(item, "文字快道不能领取视频")


class RetryTest(OutboxFixture):
    def test_retryable_failure_is_rescheduled(self):
        base = datetime(2026, 10, 19, 12, 0, 0)
        item_id = self.enqueue(now=base)
        item = self.store.claim_next(now=base)
        status = self.store.mark_retry(item.id, error="timeout", retryable=True,
                                       now=base)
        self.assertEqual(status, O.STATUS_PENDING)
        row = self.store.conn.execute(
            "SELECT attempts, next_attempt_at FROM outbox WHERE id = ?", (item_id,)
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_attempt_at"], "2026-10-19T12:00:05")

    def test_items_are_not_claimed_before_next_attempt(self):
        base = datetime(2026, 10, 19, 12, 0, 0)
        self.enqueue(now=base)
        self.store.claim_next(now=base)
        # 直接把唯一一条标记为失败并排到 5 秒后
        item_id = self.store.conn.execute("SELECT id FROM outbox").fetchone()[0]
        self.store.mark_retry(item_id, error="timeout", now=base)
        self.assertIsNone(self.store.claim_next(now=datetime(2026, 10, 19, 12, 0, 1)))
        self.assertIsNotNone(self.store.claim_next(now=datetime(2026, 10, 19, 12, 0, 6)))

    def test_permanent_failure_is_not_retried(self):
        item_id = self.enqueue()
        self.store.claim_next()
        status = self.store.mark_retry(item_id, error="401 bad token", retryable=False)
        self.assertEqual(status, O.STATUS_FAILED)
        self.assertIsNone(self.store.claim_next())

    def test_stale_sending_is_recovered(self):
        base = datetime(2026, 10, 19, 12, 0, 0)
        item_id = self.enqueue(now=base)
        self.store.claim_next(now=base, lease_seconds=10)
        self.assertEqual(self.store.recover_stale(
            now=datetime(2026, 10, 19, 12, 0, 30)), 1)
        item = self.store.claim_next(now=datetime(2026, 10, 19, 12, 0, 31))
        self.assertEqual(item.id, item_id)


class ExpiryCapacityTest(OutboxFixture):
    def test_expired_item_and_media_are_removed(self):
        media_file = self.media / "old.mp4"
        media_file.write_bytes(b"x" * 100)
        old = datetime.now() - timedelta(days=10)
        self.enqueue(kind="video", dedupe_key="old:video",
                     payload={"video_path": "old.mp4", "outbox_path": str(media_file)},
                     now=old)
        result = self.store.expire_and_prune(media_dir=self.media, max_age_days=7)
        self.assertEqual(result["expired"], 1)
        self.assertFalse(media_file.exists())
        row = self.store.conn.execute("SELECT status FROM outbox").fetchone()
        self.assertEqual(row["status"], O.STATUS_EXPIRED)

    def test_capacity_drops_old_video_never_text(self):
        video = self.media / "big.mp4"
        video.write_bytes(b"v" * 500)
        image = self.media / "big.jpg"
        image.write_bytes(b"i" * 500)
        old = datetime.now() - timedelta(minutes=5)
        self.enqueue(kind="text", dedupe_key="t:text", payload={"text": "重要"},
                     now=old)
        self.enqueue(kind="video", dedupe_key="t:video",
                     payload={"video_path": "big.mp4", "outbox_path": str(video)},
                     now=old)
        self.enqueue(kind="image", dedupe_key="t:image",
                     payload={"image_path": "big.jpg", "outbox_path": str(image)},
                     now=old)
        self.store.expire_and_prune(media_dir=self.media, max_age_days=7,
                                    max_total_bytes=600)
        statuses = {
            row["kind"]: row["status"]
            for row in self.store.conn.execute("SELECT kind, status FROM outbox")
        }
        self.assertEqual(statuses["text"], O.STATUS_PENDING)
        self.assertEqual(statuses["video"], O.STATUS_EXPIRED)
        self.assertFalse(video.exists())


class ChannelHealthTest(OutboxFixture):
    def test_channel_health_is_recorded(self):
        self.store.record_channel_result("wecom_bot", True)
        self.store.record_channel_result("wxpusher", False, "401 bad token")
        health = self.store.channel_health()
        self.assertIsNotNone(health["wecom_bot"]["last_ok_at"])
        self.assertEqual(health["wxpusher"]["last_error"], "401 bad token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
