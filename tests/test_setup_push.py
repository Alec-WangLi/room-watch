"""推送配置引导的单测，重点是 webhook 归一化。

    python tests/test_setup_push.py

背景：2026-10-18 真实踩到过一次 —— `rw push` 把完整 webhook 地址塞进了
`key=` 后面，得到 `?key=https://...?key=xxx` 这种双嵌套。文字消息照发不误
（send 接口宽松），录像上传直接 40058，症状极具误导性。这里的用例就是钉死它。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tempfile
import unittest
from unittest import mock

from roomwatch import setup_push

KEY = "00000000-0000-0000-0000-000000000000"
URL = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY}"

CONFIG_TEXT = """notify:
  channel: wxpusher
  title: 房间有人进入
  wecom_bot:
    # 群设置 → 群机器人 → 添加，复制 Webhook 地址
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME"
    send_video: true
"""


class NormalizeTest(unittest.TestCase):
    def test_full_url_passthrough(self) -> None:
        self.assertEqual(setup_push.normalize_wecom_webhook(URL), URL)

    def test_bare_key_wrapped(self) -> None:
        self.assertEqual(setup_push.normalize_wecom_webhook(KEY), URL)

    def test_strips_quotes_and_spaces(self) -> None:
        self.assertEqual(setup_push.normalize_wecom_webhook(f'  "{URL}"  '), URL)

    def test_double_nested_url_is_unwrapped(self) -> None:
        """旧版本写坏的值也能救回来：取最后一个 key=。"""
        broken = f"{URL.split('key=')[0]}key={URL}"
        self.assertEqual(setup_push.normalize_wecom_webhook(broken), URL)

    def test_extra_query_dropped(self) -> None:
        self.assertEqual(
            setup_push.normalize_wecom_webhook(f"{URL}&foo=bar"), URL)

    def test_empty(self) -> None:
        self.assertEqual(setup_push.normalize_wecom_webhook(""), "")
        self.assertEqual(setup_push.normalize_wecom_webhook("   "), "")


class WriteConfigTest(unittest.TestCase):
    """凭据只进 config.local.yaml，config.yaml 一个字都不许改。"""

    def _setup(self, existing_local: str = "") -> tuple:
        d = tempfile.mkdtemp()
        main = _os.path.join(d, "config.yaml")
        local = _os.path.join(d, "config.local.yaml")
        with open(main, "w", encoding="utf-8") as f:
            f.write(CONFIG_TEXT)
        if existing_local:
            with open(local, "w", encoding="utf-8") as f:
                f.write(existing_local)
        return d, main, local

    def _write(self, channel: str, creds: dict, existing_local: str = "") -> tuple:
        d, main, local = self._setup(existing_local)
        with mock.patch.object(setup_push, "LOCAL_CONFIG", local):
            changes = setup_push.write_config(channel, creds)
        with open(local, "r", encoding="utf-8") as f:
            wrote = f.read()
        with open(main, "r", encoding="utf-8") as f:
            kept = f.read()
        return wrote, kept, changes

    def test_wecom_webhook_written_not_nested(self) -> None:
        wrote, _, changes = self._write(
            "wecom_bot", {"webhook": setup_push.normalize_wecom_webhook(URL)}
        )
        self.assertEqual(wrote.count("key="), 1, "只能有一个 key=，否则就是双嵌套")
        self.assertIn(URL, wrote)
        self.assertIn("channel: wecom_bot", wrote)
        self.assertIn("已写入 webhook", " ".join(changes))

    def test_main_config_untouched(self) -> None:
        _, kept, _ = self._write("wecom_bot", {"webhook": URL})
        self.assertEqual(kept, CONFIG_TEXT, "config.yaml 是入库文件，不能被写")

    def test_local_file_is_marked_not_ignored_by_mistake(self) -> None:
        wrote, _, _ = self._write("wecom_bot", {"webhook": URL})
        self.assertIn("不入库", wrote)

    def test_switching_channel_keeps_other_creds(self) -> None:
        first, _, _ = self._write("wecom_bot", {"webhook": URL})
        second, _, _ = self._write(
            "wxpusher", {"token": "AT_real", "uid": "UID_real"}, existing_local=first
        )
        self.assertIn("channel: wxpusher", second)
        self.assertIn("AT_real", second)
        self.assertIn("UID_real", second)
        self.assertIn(KEY, second, "换渠道不该把之前的企业微信 webhook 删掉")


if __name__ == "__main__":
    unittest.main()
