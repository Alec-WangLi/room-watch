"""多渠道扇出与投递结果分类的单测。

    python tests/test_delivery.py

这里锁的是 outbox 能否正确判断「重试还是放弃」：把 401 当可重试会让凭据
错误永远刷屏，把 5xx 当永久失败会让一次抖动的服务端错误永久丢告警。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from roomwatch import delivery as D
from roomwatch import notifier as N
from roomwatch import outbox as O
from roomwatch import videopush as VP


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class ChannelsTest(unittest.TestCase):
    def test_legacy_single_channel_still_works(self):
        self.assertEqual(D.enabled_channels({"channel": "wecom_bot"}), ["wecom_bot"])

    def test_channels_list_wins(self):
        self.assertEqual(
            D.enabled_channels({"channel": "wecom_bot",
                                "channels": ["wxpusher", "serverchan"]}),
            ["wxpusher", "serverchan"],
        )

    def test_comma_string_and_dedupe(self):
        self.assertEqual(
            D.enabled_channels({"channels": "wecom_bot, wxpusher,wecom_bot"}),
            ["wecom_bot", "wxpusher"],
        )

    def test_none_and_empty_disable_all(self):
        self.assertEqual(D.enabled_channels({"channel": "none"}), [])
        self.assertEqual(D.enabled_channels({"channels": []}), [])
        self.assertEqual(D.enabled_channels({}), [])

    def test_unknown_channel_is_ignored(self):
        self.assertEqual(D.enabled_channels({"channels": ["telegram"]}), [])


class BuildItemsTest(unittest.TestCase):
    def test_fanout_text_image_video(self):
        items = D.build_items(
            {"channels": ["wecom_bot", "wxpusher"],
             "wecom_bot": {"send_video": True}},
            event_id="e1", text="有人", image_path="a.jpg", video_path="b.mp4",
        )
        self.assertEqual(
            [(i["channel"], i["kind"]) for i in items],
            [("wecom_bot", "text"), ("wecom_bot", "image"),
             ("wecom_bot", "video"), ("wxpusher", "text")],
        )

    def test_dedupe_keys_are_stable(self):
        cfg = {"channels": ["wecom_bot"]}
        first = D.build_items(cfg, event_id="e1", text="x", image_path="a.jpg")
        second = D.build_items(cfg, event_id="e1", text="x", image_path="a.jpg")
        self.assertEqual([i["dedupe_key"] for i in first],
                         [i["dedupe_key"] for i in second])

    def test_video_respects_send_video_switch(self):
        items = D.build_items(
            {"channels": ["wecom_bot"], "wecom_bot": {"send_video": False}},
            event_id="e1", text="x", video_path="b.mp4",
        )
        self.assertEqual([i["kind"] for i in items], ["text"])


class NotifierDeliverTest(unittest.TestCase):
    def _notifier(self, **bot):
        return N.Notifier({"channel": "wecom_bot",
                           "wecom_bot": {"webhook": "https://x.test/send?key=K", **bot}})

    def test_text_success(self):
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"errcode": 0})):
            result = self._notifier().deliver("wecom_bot", "text", {"text": "x"})
        self.assertTrue(result.ok)

    def test_http_500_is_retryable(self):
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({}, status=500)):
            result = self._notifier().deliver("wecom_bot", "text", {"text": "x"})
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)

    def test_http_401_is_permanent(self):
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({}, status=401)):
            result = self._notifier().deliver("wecom_bot", "text", {"text": "x"})
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)

    def test_missing_credentials_is_permanent(self):
        n = N.Notifier({"channel": "wecom_bot", "wecom_bot": {"webhook": ""}})
        result = n.deliver("wecom_bot", "text", {"text": "x"})
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)

    def test_image_item_sends_only_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "a.jpg"
            image.write_bytes(b"jpeg")
            with mock.patch.object(N.requests, "post",
                                   return_value=FakeResponse({"errcode": 0})) as post:
                result = self._notifier().deliver(
                    "wecom_bot", "image", {"image_path": str(image)})
            self.assertTrue(result.ok)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["json"]["msgtype"], "image")

    def test_missing_image_is_permanent(self):
        result = self._notifier().deliver(
            "wecom_bot", "image", {"image_path": "nope.jpg"})
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)


class WebhookChannelTest(unittest.TestCase):
    def _notifier(self, **cfg):
        return N.Notifier({"channels": ["webhook"],
                           "webhook": {"url": "https://hook.test/x", **cfg}})

    def test_text_payload_shape(self):
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({})) as post:
            result = self._notifier().deliver(
                "webhook", "text",
                {"text": "有人", "owner": "other", "event_ts": "2026-10-19T12:00:00",
                 "severity": "event"})
        self.assertTrue(result.ok)
        body = post.call_args.kwargs["json"]
        for key in ("event", "kind", "severity", "time", "text", "owner",
                    "has_image", "image_base64"):
            self.assertIn(key, body)
        self.assertEqual(body["owner"], "other")

    def test_small_image_is_inlined(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "a.jpg"
            image.write_bytes(b"jpeg-small")
            with mock.patch.object(N.requests, "post",
                                   return_value=FakeResponse({})) as post:
                result = self._notifier().deliver(
                    "webhook", "image", {"image_path": str(image)})
        self.assertTrue(result.ok)
        self.assertTrue(post.call_args.kwargs["json"]["has_image"])
        self.assertTrue(post.call_args.kwargs["json"]["image_base64"])

    def test_oversized_image_is_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "a.jpg"
            image.write_bytes(b"x" * 100)
            with mock.patch.object(N.requests, "post",
                                   return_value=FakeResponse({})) as post:
                result = self._notifier(max_image_mb=0).deliver(
                    "webhook", "image", {"image_path": str(image)})
        self.assertTrue(result.ok)
        self.assertFalse(post.call_args.kwargs["json"]["has_image"])


class VideoDeliverTest(unittest.TestCase):
    def test_video_success_removes_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"v" * 100)
            notifier = N.Notifier({
                "channels": ["wecom_bot"],
                "wecom_bot": {"webhook": "https://x.test/send?key=K",
                              "send_video": True},
            })
            def fake_transcode(src, dst, *args, **kwargs):
                Path(dst).write_bytes(b"small")
                return dst
            with mock.patch.object(N.shutil, "which", return_value="ffmpeg"), \
                    mock.patch.object(VP, "transcode_under", side_effect=fake_transcode), \
                    mock.patch.object(VP, "upload_media", return_value="mid"), \
                    mock.patch.object(VP, "send_file_message", return_value=True):
                result = notifier.deliver(
                    "wecom_bot", "video",
                    {"video_path": str(source), "_item_id": 7},
                    outbox_dir=str(root / "outbox"))
            self.assertTrue(result.ok)
            self.assertFalse((root / "outbox" / "item-7.mp4").exists())

    def test_missing_video_is_permanent(self):
        notifier = N.Notifier({"wecom_bot": {"webhook": "https://x.test/send?key=K"}})
        result = notifier.deliver("wecom_bot", "video", {"video_path": "nope.mp4"})
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)


class DeliveryWorkerTest(unittest.TestCase):
    class FakeDispatcher:
        def __init__(self, results=None):
            self.results = list(results or [])
            self.calls = []

        def deliver(self, item):
            self.calls.append(item)
            result = self.results.pop(0) if self.results else O.DeliveryResult(True)
            if isinstance(result, Exception):
                raise result
            return result

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

    def _worker(self, results=None, **kwargs):
        return D.DeliveryWorker(self.db, self.FakeDispatcher(results),
                                media_dir=self.media, **kwargs)

    def _enqueue(self, kind="text", dedupe="e1:text"):
        return self.store.enqueue(channel="wecom_bot", kind=kind, payload={"text": "x"},
                                  event_id="e1", dedupe_key=dedupe)

    def test_delivers_and_marks_done(self):
        self._enqueue()
        worker = self._worker()
        self.assertTrue(worker.run_once(self.store))
        self.assertEqual(self.store.stats()["delivered"], 1)
        self.assertFalse(worker.run_once(self.store))

    def test_permanent_failure_is_recorded(self):
        self._enqueue()
        worker = self._worker([O.DeliveryResult(False, retryable=False, error="401")])
        worker.run_once(self.store)
        self.assertEqual(self.store.stats()["failed_permanent"], 1)

    def test_exception_is_rescheduled_not_raised(self):
        self._enqueue()
        worker = self._worker([RuntimeError("boom")])
        self.assertTrue(worker.run_once(self.store))
        row = self.store.conn.execute(
            "SELECT status, attempts, last_error FROM outbox").fetchone()
        self.assertEqual(row["status"], O.STATUS_PENDING)
        self.assertEqual(row["attempts"], 1)
        self.assertIn("boom", row["last_error"])

    def test_flush_drains_until_empty(self):
        for i in range(3):
            self._enqueue(dedupe=f"e{i}:text")
        worker = self._worker()
        self.assertEqual(worker.flush(10), 3)
        self.assertEqual(self.store.stats()["delivered"], 3)

    def test_kinds_filter(self):
        self._enqueue(kind="video", dedupe="e1:video")
        worker = self._worker()
        self.assertEqual(worker.flush(10), 0)

    def test_thread_start_delivers_and_stops(self):
        self._enqueue()
        worker = self._worker(poll_seconds=0.1)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                reader = O.OutboxStore(self.db)
                delivered = reader.stats()["delivered"]
                reader.close()
                if delivered:
                    break
                time.sleep(0.05)
        finally:
            worker.stop()
        self.assertEqual(delivered, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

