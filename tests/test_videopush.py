"""企业微信录像推送的单测。

    python tests/test_videopush.py

不联网、不真跑 ffmpeg：ffmpeg 与 HTTP 都换成假的，只验证
「命令怎么拼的、压不下去时怎么退、上传与发消息的调用对不对」。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile
import unittest
from unittest import mock

from roomwatch import videopush


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def write_file(path: str, size: int) -> None:
    with open(path, "wb") as f:
        f.write(b"x" * size)


class WebhookKeyTest(unittest.TestCase):
    def test_extracts_key(self) -> None:
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
        self.assertEqual(videopush.webhook_key(url), "abc-123")

    def test_missing_or_empty(self) -> None:
        self.assertIsNone(videopush.webhook_key(""))
        self.assertIsNone(videopush.webhook_key("https://qyapi.weixin.qq.com/send"))
        self.assertIsNone(videopush.webhook_key("https://x.com/send?key="))


class TranscodeCmdTest(unittest.TestCase):
    def test_audio_on_has_aac(self) -> None:
        cmd = videopush.build_transcode_cmd("ffmpeg", "a.mp4", "b.mp4", 854, 12, 30, True)
        self.assertEqual(cmd[:3], ["ffmpeg", "-y", "-i"])
        self.assertIn("fps=12,scale=854:-2", cmd)
        self.assertIn("-c:a", cmd)
        self.assertIn("aac", cmd)
        self.assertEqual(cmd[-1], "b.mp4")

    def test_audio_off_uses_an(self) -> None:
        cmd = videopush.build_transcode_cmd("ffmpeg", "a.mp4", "b.mp4", audio=False)
        self.assertIn("-an", cmd)
        self.assertNotIn("-c:a", cmd)

    def test_crf_and_faststart(self) -> None:
        cmd = videopush.build_transcode_cmd("ffmpeg", "a.mp4", "b.mp4", crf=36)
        self.assertIn("36", cmd)
        self.assertIn("+faststart", cmd)


class TranscodeUnderTest(unittest.TestCase):
    def _fake_run(self, size: int, code: int = 0):
        """假 ffmpeg：按给定大小产出目标文件，并记录 crf。"""
        calls = []

        def _run(cmd, timeout):
            calls.append(cmd)
            if code == 0:
                write_file(cmd[-1], size)
            return code, ""

        return calls, _run

    def test_first_crf_fits(self) -> None:
        calls, run = self._fake_run(1024)
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "out.mp4")
            with mock.patch.object(videopush, "_run", run):
                got = videopush.transcode_under("src.mp4", dst, "ffmpeg", 10 * 1024 * 1024)
            self.assertEqual(got, dst)
        self.assertEqual(len(calls), 1)

    def test_climbs_ladder_then_gives_up(self) -> None:
        calls, run = self._fake_run(50 * 1024 * 1024)  # 永远超标
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "out.mp4")
            with mock.patch.object(videopush, "_run", run):
                got = videopush.transcode_under("src.mp4", dst, "ffmpeg", 1024)
            self.assertIsNone(got)
            self.assertFalse(os.path.exists(dst), "放弃时要把半成品删掉")
        self.assertEqual(len(calls), len(videopush.CRF_LADDER))

    def test_ffmpeg_failure_returns_none(self) -> None:
        calls, run = self._fake_run(10, code=1)
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "out.mp4")
            with mock.patch.object(videopush, "_run", run):
                self.assertIsNone(videopush.transcode_under("src.mp4", dst, "ffmpeg", 1024))
        self.assertEqual(len(calls), 1, "命令失败不该再试下一级")


class HttpTest(unittest.TestCase):
    def test_upload_media_posts_multipart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.mp4")
            write_file(p, 16)
            with mock.patch.object(videopush.requests, "post", return_value=FakeResponse(
                    {"errcode": 0, "media_id": "MID"})) as post:
                got = videopush.upload_media(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K", p)
        self.assertEqual(got, "MID")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["params"], {"key": "K", "type": "file"})
        self.assertIn("media", kwargs["files"])

    def test_upload_media_rejects_bad_webhook(self) -> None:
        self.assertIsNone(videopush.upload_media("https://no-key.example/send", "nope.mp4"))

    def test_upload_media_reports_errcode(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.mp4")
            write_file(p, 16)
            with mock.patch.object(videopush.requests, "post",
                                   return_value=FakeResponse({"errcode": 40001})):
                self.assertIsNone(videopush.upload_media(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K", p))

    def test_send_file_payload(self) -> None:
        with mock.patch.object(videopush.requests, "post",
                               return_value=FakeResponse({"errcode": 0})) as post:
            ok = videopush.send_file_message("https://hook", "MID")
        self.assertTrue(ok)
        self.assertEqual(
            post.call_args.kwargs["json"], {"msgtype": "file", "file": {"media_id": "MID"}})

    def test_send_text_payload(self) -> None:
        with mock.patch.object(videopush.requests, "post",
                               return_value=FakeResponse({"errcode": 0})) as post:
            ok = videopush.send_text_message("https://hook", "hello")
        self.assertTrue(ok)
        self.assertEqual(
            post.call_args.kwargs["json"], {"msgtype": "text", "text": {"content": "hello"}})


class PusherEnabledTest(unittest.TestCase):
    def _cfg(self, channel="wecom_bot", webhook="https://qyapi.weixin.qq.com/send?key=K",
             send_video=True):
        return {"channel": channel, "wecom_bot": {"webhook": webhook, "send_video": send_video}}

    def test_enabled(self) -> None:
        self.assertTrue(videopush.VideoPusher(self._cfg()).enabled)

    def test_disabled_cases(self) -> None:
        self.assertFalse(videopush.VideoPusher(self._cfg(channel="wxpusher")).enabled)
        self.assertFalse(videopush.VideoPusher(self._cfg(webhook="...key=REPLACE_ME")).enabled)
        self.assertFalse(videopush.VideoPusher(self._cfg(webhook="")).enabled)
        self.assertFalse(videopush.VideoPusher(self._cfg(send_video=False)).enabled)

    def test_submit_noop_when_disabled(self) -> None:
        p = videopush.VideoPusher(self._cfg(send_video=False))
        self.assertFalse(p.submit("a.mp4", "cap"))
        self.assertIsNone(p._queue, "未启用时不该建队列")


class PusherSendTest(unittest.TestCase):
    def _pusher(self) -> videopush.VideoPusher:
        cfg = {"channel": "wecom_bot",
               "wecom_bot": {"webhook": "https://qyapi.weixin.qq.com/send?key=K",
                             "send_video": True, "video": {"max_mb": 18}}}
        return videopush.VideoPusher(cfg)

    def test_send_compresses_uploads_and_sends(self) -> None:
        p = self._pusher()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "090000.mp4")
            write_file(src, 40 * 1024 * 1024)
            p.cache_dir = os.path.join(d, "cache")
            small = os.path.join(p.cache_dir, "090000-wx.mp4")

            def fake_transcode(_src, dst, _ffmpeg, _cap, _w, _f, _a, **kw):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                write_file(dst, 1024)
                return dst

            with mock.patch.object(videopush, "transcode_under", fake_transcode), \
                 mock.patch.object(videopush, "upload_media", return_value="MID") as up, \
                 mock.patch.object(videopush, "send_file_message", return_value=True) as sf, \
                 mock.patch.object(videopush, "send_text_message", return_value=True) as st:
                ok = p.send(src, "09:00:00 房间录像")
        self.assertTrue(ok)
        self.assertEqual(up.call_args.args[1], small)
        self.assertTrue(sf.called)
        self.assertTrue(st.called)
        self.assertFalse(os.path.exists(small), "临时压缩文件要删掉")

    def test_send_skips_missing_file(self) -> None:
        self.assertFalse(self._pusher().send(os.path.join("no", "such.mp4")))

    def test_send_no_ffmpeg(self) -> None:
        p = self._pusher()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.mp4")
            write_file(src, 8)
            with mock.patch.object(videopush.shutil, "which", return_value=None):
                self.assertFalse(p.send(src, "cap"))

    def test_too_big_falls_back_to_text(self) -> None:
        """压不进上限时不能假装成功，至少把「录像在哪」告诉用户。"""
        p = self._pusher()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.mp4")
            write_file(src, 30 * 1024 * 1024)
            p.cache_dir = os.path.join(d, "cache")
            with mock.patch.object(videopush, "transcode_under", return_value=None), \
                 mock.patch.object(videopush.shutil, "which", return_value="ffmpeg"), \
                 mock.patch.object(videopush, "send_text_message", return_value=True) as st:
                ok = p.send(src, "cap")
        self.assertFalse(ok)
        self.assertTrue(st.called)
        self.assertIn("仅本机可看", st.call_args.args[1])

    def test_submit_runs_in_background(self) -> None:
        p = self._pusher()
        seen = []
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.mp4")
            write_file(src, 8)
            with mock.patch.object(p, "send", side_effect=lambda v, c: seen.append(v) or True):
                self.assertTrue(p.submit(src, "cap"))
                p._thread.join(timeout=5.0)
        self.assertEqual(seen, [src])

    def test_queue_full_drops(self) -> None:
        p = self._pusher()
        with mock.patch.object(p, "send", return_value=True):
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "a.mp4")
                write_file(src, 8)
                for _ in range(20):
                    p.submit(src, "cap")
        self.assertLessEqual(p._queue.qsize(), 8)


if __name__ == "__main__":
    unittest.main()
