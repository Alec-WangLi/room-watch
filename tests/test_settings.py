"""界面设置层的单测：读写 config.local.yaml、密钥掩码、类型校验、复读校验。

    python tests/test_settings.py

为什么写 local 而不是 config.yaml：安装包 `[Files]` 是 `ignoreversion` 全量覆盖，
升级会把 config.yaml 冲掉。用户点界面存的东西必须活过升级。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from roomwatch import settings

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1fcbf0dc-d33c-4dac-83d6-10c1c99a5179"


def make_project(root: Path, main_extra: str = "") -> Path:
    """造一个最小项目：config.yaml 带真实默认值，足够 load_config 合并。"""
    cfg = (
        "notify:\n"
        "  channel: none\n"
        "  wecom_bot:\n"
        '    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME"\n'
        "    send_video: true\n"
        "  wxpusher:\n"
        '    app_token: "AT_xxx_replace_me"\n'
        '    uid: "UID_xxx_replace_me"\n'
        "  serverchan:\n"
        '    sendkey: "SCTxxx_replace_me"\n'
        "identity:\n"
        "  threshold: 0.45\n"
        "  unknown_policy: alert\n"
        "  on_owner: skip\n"
        "voice:\n"
        "  enabled: true\n"
        '  text: "您已进入监控区域，请注意"\n'
        "  cooldown_seconds: 60\n"
        "  volume_mode: off\n"
        "camera:\n"
        "  index: 0\n"
    ) + main_extra
    (root / "config.yaml").write_text(cfg, encoding="utf-8")
    return root


class SpecTest(unittest.TestCase):
    def test_spec_covers_all_tabs(self) -> None:
        sections = {f.section for f in settings.SPEC.values()}
        self.assertEqual(sections, set(settings.sections()))
        self.assertEqual(len(sections), 6)

    def test_credentials_are_flagged_secret(self) -> None:
        for key in ("notify.wecom_bot.webhook", "notify.wxpusher.app_token",
                    "notify.wxpusher.uid", "notify.serverchan.sendkey",
                    "gallery.token", "camera.source"):
            self.assertTrue(settings.SPEC[key].secret, key)


class SectionConsistencyTest(unittest.TestCase):
    def test_window_tabs_match_backend_sections(self) -> None:
        """界面页签与后端分组必须一致，否则某项设置会「无处可改」。"""
        from roomwatch import gui_settings

        self.assertEqual(tuple(settings.sections()), gui_settings.SECTIONS)


class CoerceTest(unittest.TestCase):
    def test_string_number_is_converted(self) -> None:
        field = settings.SPEC["voice.cooldown_seconds"]
        self.assertEqual(settings.coerce(field, "30"), 30.0)

    def test_bool_accepts_common_spellings(self) -> None:
        field = settings.SPEC["voice.enabled"]
        for raw in ("1", "true", "on", "是", True):
            self.assertIs(settings.coerce(field, raw), True, raw)
        for raw in ("0", "false", "off", "否", False):
            self.assertIs(settings.coerce(field, raw), False, raw)

    def test_choice_is_validated(self) -> None:
        field = settings.SPEC["identity.unknown_policy"]
        with self.assertRaises(ValueError):
            settings.coerce(field, "whatever")

    def test_range_is_validated(self) -> None:
        field = settings.SPEC["identity.threshold"]
        self.assertEqual(settings.coerce(field, "0.55"), 0.55)
        for bad in ("0", "1", "1.5", "-0.2"):
            with self.assertRaises(ValueError):
                settings.coerce(field, bad)


class SnapshotTest(unittest.TestCase):
    def test_plain_values_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertEqual(snap["values"]["notify.channel"], "none")
        self.assertEqual(snap["values"]["identity.threshold"], 0.45)

    def test_placeholder_credential_counts_as_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertFalse(snap["secrets"]["notify.wecom_bot.webhook"]["set"])
        self.assertEqual(snap["secrets"]["notify.wecom_bot.webhook"]["tail"], "")

    def test_real_credential_is_masked_never_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.wecom_bot.webhook": WEBHOOK})
            snap = settings.snapshot(root)
            dumped = json.dumps(snap, ensure_ascii=False)
        info = snap["secrets"]["notify.wecom_bot.webhook"]
        self.assertTrue(info["set"])
        self.assertEqual(info["tail"], "…5179")
        self.assertNotIn("1fcbf0dc", dumped)   # 全文绝不出现在返回值里


class ApplyTest(unittest.TestCase):
    def test_plain_key_goes_to_local_and_takes_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"identity.threshold": "0.6"})
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
            self.assertEqual(data["identity"]["threshold"], 0.6)
            from roomwatch.config import load_config
            self.assertEqual(load_config(str(root / "config.yaml"))["identity"]["threshold"], 0.6)

    def test_existing_local_keys_are_preserved(self) -> None:
        """界面上改推送，不能把向导写的摄像头密码冲掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "config.local.yaml").write_text(
                "camera:\n  source: rtsp://user:pw@10.0.0.2:554/live\n", encoding="utf-8")
            settings.apply(root, {"notify.channel": "wecom_bot"})
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["camera"]["source"], "rtsp://user:pw@10.0.0.2:554/live")
        self.assertEqual(data["notify"]["channel"], "wecom_bot")

    def test_unknown_key_is_rejected_and_nothing_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"detector.conf": 0.9})
            self.assertFalse(result["ok"])
            self.assertIn("detector.conf", result["errors"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_bad_value_blocks_whole_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"identity.threshold": "9",
                                           "notify.channel": "wecom_bot"})
            self.assertFalse(result["ok"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_blank_secret_means_keep_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.wecom_bot.webhook": WEBHOOK})
            settings.apply(root, {"notify.wecom_bot.webhook": "  "})
            snap = settings.snapshot(root)
        self.assertTrue(snap["secrets"]["notify.wecom_bot.webhook"]["set"])

    def test_readback_mismatch_is_reported(self) -> None:
        """写完必须复读；读回来不一样就是失败，不许静默成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            real = settings.write_local

            def sabotage(path: Path, data: dict) -> None:
                real(path, data)
                (root / "config.local.yaml").write_text("notify:\n  channel: none\n",
                                                        encoding="utf-8")

            settings.write_local = sabotage  # type: ignore[assignment]
            try:
                result = settings.apply(root, {"notify.channel": "wecom_bot"})
            finally:
                settings.write_local = real  # type: ignore[assignment]
        self.assertFalse(result["ok"])
        self.assertTrue(any("复读" in msg for msg in result["errors"].values()))


class GallerySettingsTest(unittest.TestCase):
    def test_lan_requires_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"gallery.host": "0.0.0.0"})
            self.assertFalse(result["ok"])
            self.assertIn("gallery.token", result["errors"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_lan_with_short_password_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"gallery.host": "0.0.0.0",
                                           "gallery.token": "short"})
            self.assertFalse(result["ok"])
            self.assertIn("gallery.token", result["errors"])

    def test_lan_with_password_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"gallery.host": "0.0.0.0",
                                           "gallery.token": "a-strong-passphrase"})
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["gallery"]["host"], "0.0.0.0")
        self.assertEqual(data["gallery"]["token"], "a-strong-passphrase")

    def test_gallery_info_shows_local_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            info = settings.gallery_info(root)
        self.assertFalse(info["lan_enabled"])
        self.assertEqual(info["local_url"], "http://127.0.0.1:8765/")
        self.assertEqual(info["lan_urls"], [])

    def test_gallery_info_lists_lan_urls_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"gallery.host": "0.0.0.0",
                                  "gallery.token": "a-strong-passphrase"})
            with mock.patch.object(settings, "_lan_addresses",
                                   return_value=[("WLAN", "192.168.1.3")]):
                info = settings.gallery_info(root)
        self.assertTrue(info["lan_enabled"])
        self.assertTrue(info["token_set"])
        self.assertEqual(info["lan_urls"][0]["url"], "http://192.168.1.3:8765/")

    def test_unset_removes_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"camera.source": "rtsp://u:p@10.0.0.2:554/live"})
            result = settings.unset(root, ["camera.source", "camera.backend"])
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("camera", data)


if __name__ == "__main__":
    unittest.main()
