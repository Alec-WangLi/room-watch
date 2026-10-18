"""推送层单测：企业微信图片/文字的请求形状与容错。

    python tests/test_notifier.py

重点是一条真实事故的回归：心跳推送曾把正文当图片字节传进来
（`send("监控在线", text)`），base64 直接抛 TypeError，整条消息发不出去。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import unittest
from unittest import mock

from roomwatch import notifier as N

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K"


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def make_notifier(**bot) -> N.Notifier:
    cfg = {"channel": "wecom_bot", "title": "房间有人进入",
           "wecom_bot": {"webhook": WEBHOOK, **bot}}
    return N.Notifier(cfg)


class WecomBotTest(unittest.TestCase):
    def test_image_then_text(self) -> None:
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"errcode": 0})) as post:
            ok = make_notifier().send("有人进入", image_bytes=b"jpeg-bytes")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        first = post.call_args_list[0].kwargs["json"]
        self.assertEqual(first["msgtype"], "image")
        self.assertIn("base64", first["image"])
        self.assertEqual(first["image"]["md5"], N.hashlib.md5(b"jpeg-bytes").hexdigest())
        second = post.call_args_list[1].kwargs["json"]
        self.assertEqual(second["msgtype"], "text")
        self.assertEqual(second["text"]["content"], "有人进入")

    def test_text_only(self) -> None:
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"errcode": 0})) as post:
            ok = make_notifier().send("只有文字")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["msgtype"], "text")

    def test_string_in_image_slot_does_not_crash(self) -> None:
        """回归：正文被误传进 image_bytes 时，至少要把文字发出去。"""
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"errcode": 0})) as post:
            ok = make_notifier().send("监控在线", "副标题被误传")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["msgtype"], "text")

    def test_placeholder_webhook_skips(self) -> None:
        cfg = {"channel": "wecom_bot",
               "wecom_bot": {"webhook": "https://x/send?key=REPLACE_ME"}}
        with mock.patch.object(N.requests, "post") as post:
            self.assertFalse(N.Notifier(cfg).send("x"))
        post.assert_not_called()

    def test_errcode_nonzero_is_failure(self) -> None:
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"errcode": 93000})):
            self.assertFalse(make_notifier().send("x"))

    def test_network_error_returns_false(self) -> None:
        with mock.patch.object(N.requests, "post", side_effect=OSError("断网")):
            self.assertFalse(make_notifier().send("x"))


class ChannelDispatchTest(unittest.TestCase):
    def test_none_channel_just_logs(self) -> None:
        with mock.patch.object(N.requests, "post") as post:
            self.assertTrue(N.Notifier({"channel": "none"}).send("x"))
        post.assert_not_called()

    def test_unknown_channel_returns_false(self) -> None:
        self.assertFalse(N.Notifier({"channel": "telegram"}).send("x"))

    def test_wxpusher_requires_creds(self) -> None:
        with mock.patch.object(N.requests, "post") as post:
            self.assertFalse(N.Notifier({"channel": "wxpusher"}).send("x"))
        post.assert_not_called()

    def test_serverchan_posts_form(self) -> None:
        cfg = {"channel": "serverchan", "title": "T", "serverchan": {"sendkey": "SCT123"}}
        with mock.patch.object(N.requests, "post",
                               return_value=FakeResponse({"code": 0})) as post:
            self.assertTrue(N.Notifier(cfg).send("正文"))
        self.assertIn("SCT123", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["data"]["desp"], "正文")


if __name__ == "__main__":
    unittest.main()
