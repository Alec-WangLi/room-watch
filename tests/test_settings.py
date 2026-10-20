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
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.assertEqual(len(sections), 9)
        self.assertIn("录像", sections)
        self.assertIn("更新", sections)

    def test_credentials_are_flagged_secret(self) -> None:
        for key in ("notify.wxpusher.app_token",
                    "notify.wxpusher.uid", "notify.serverchan.sendkey",
                    "notify.webhook.url", "monitor.heartbeat_url",
                    "monitor.heartbeat_fail_url", "monitor.heartbeat_start_url",
                    "camera.source"):
            self.assertTrue(settings.SPEC[key].secret, key)

    def test_wecom_webhook_is_visible_on_purpose(self) -> None:
        """Webhook 要直接显示在输入框里，否则用户以为没填。"""
        self.assertFalse(settings.SPEC["notify.wecom_bot.webhook"].secret)

    def test_gallery_password_is_visible_on_purpose(self) -> None:
        """相册密码要输到手机上，藏起来只会让人没法登录。"""
        self.assertFalse(settings.SPEC["gallery.token"].secret)


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
        self.assertEqual(snap["values"]["notify.channels"], "")
        self.assertEqual(snap["values"]["identity.threshold"], 0.45)

    def test_placeholder_webhook_is_shown_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertEqual(snap["values"]["notify.wecom_bot.webhook"], "")

    def test_wecom_webhook_is_returned_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.wecom_bot.webhook": WEBHOOK})
            snap = settings.snapshot(root)
            dumped = json.dumps(snap, ensure_ascii=False)
        self.assertEqual(snap["values"]["notify.wecom_bot.webhook"], WEBHOOK)
        self.assertNotIn("notify.wecom_bot.webhook", snap["secrets"])
        self.assertIn("1fcbf0dc", dumped)

    def test_other_credentials_are_still_masked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.wxpusher.app_token": "AT_REAL_12345678"})
            snap = settings.snapshot(root)
            dumped = json.dumps(snap, ensure_ascii=False)
        info = snap["secrets"]["notify.wxpusher.app_token"]
        self.assertTrue(info["set"])
        self.assertNotIn("AT_REAL_12345678", dumped)


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
            settings.apply(root, {"notify.channels": "wecom_bot"})
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["camera"]["source"], "rtsp://user:pw@10.0.0.2:554/live")
        self.assertEqual(data["notify"]["channels"], ["wecom_bot"])

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
                                           "notify.channels": "wecom_bot"})
            self.assertFalse(result["ok"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_blank_secret_means_keep_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.wxpusher.app_token": "AT_real_12345678"})
            settings.apply(root, {"notify.wxpusher.app_token": "  "})
            snap = settings.snapshot(root)
        self.assertTrue(snap["secrets"]["notify.wxpusher.app_token"]["set"])

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
                result = settings.apply(root, {"notify.channels": "wecom_bot"})
            finally:
                settings.write_local = real  # type: ignore[assignment]
        self.assertFalse(result["ok"])
        self.assertTrue(any("复读" in msg for msg in result["errors"].values()))


class MultiChannelSettingTest(unittest.TestCase):
    def test_multi_coerce_dedupes_and_validates(self) -> None:
        field = settings.SPEC["notify.channels"]
        self.assertEqual(settings.coerce(field, "wecom_bot, wxpusher,wecom_bot"),
                         ["wecom_bot", "wxpusher"])
        with self.assertRaises(ValueError):
            settings.coerce(field, "wecom_bot,telegram")

    def test_channels_are_written_as_yaml_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"notify.channels": "wecom_bot,wxpusher"})
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
            snap = settings.snapshot(root)
        self.assertEqual(data["notify"]["channels"], ["wecom_bot", "wxpusher"])
        self.assertEqual(snap["values"]["notify.channels"], "wecom_bot,wxpusher")
        self.assertEqual(snap["channels"], ["wecom_bot", "wxpusher"])

    def test_legacy_channel_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertEqual(snap["values"]["notify.channels"], "")
        self.assertEqual(snap["channels"], [])

    def test_unconfigured_channel_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"notify.channels": "wecom_bot"})
            snap = settings.snapshot(root)
        self.assertIn("wecom_bot", snap["channel_issues"])
        self.assertIn("webhook", snap["channel_issues"]["wecom_bot"])


class HeartbeatSettingTest(unittest.TestCase):
    def test_heartbeat_info_reads_merged_config_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "config.local.yaml").write_text(
                "monitor:\n  heartbeat_url: https://hc.test/abc\n", encoding="utf-8")
            state = root / "var" / "state"
            state.mkdir(parents=True)
            (state / "guardian_state.json").write_text(json.dumps({
                "last_heartbeat_at": "2026-10-19T12:00:00",
                "last_heartbeat_ok": True,
                "last_heartbeat_error": "",
            }), encoding="utf-8")
            snap = settings.snapshot(root)
        self.assertTrue(snap["heartbeat"]["configured"])
        self.assertEqual(snap["heartbeat"]["interval_seconds"], 300)
        self.assertEqual(snap["heartbeat"]["last_at"], "2026-10-19T12:00:00")

    def test_outbox_info_is_zero_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertEqual(snap["outbox"]["pending"], 0)
        self.assertEqual(snap["outbox"]["channels"], {})
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

    def test_gallery_password_is_returned_in_plain_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {"gallery.token": "a-strong-passphrase"})
            snap = settings.snapshot(root)
        self.assertEqual(snap["values"]["gallery.token"], "a-strong-passphrase")
        self.assertNotIn("gallery.token", snap["secrets"])

    def test_lan_addresses_skip_virtual_and_public_interfaces(self) -> None:
        addrs = {
            "WLAN": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.3")],
            "vEthernet (Default Switch)": [
                SimpleNamespace(family=socket.AF_INET, address="172.28.208.1")],
            "FlClash": [SimpleNamespace(family=socket.AF_INET, address="28.0.0.1")],
            "vEthernet (WSL)": [
                SimpleNamespace(family=socket.AF_INET, address="192.168.112.1")],
        }
        stats = {name: SimpleNamespace(isup=True) for name in addrs}
        with mock.patch("psutil.net_if_addrs", return_value=addrs), \
                mock.patch("psutil.net_if_stats", return_value=stats):
            found = settings._lan_addresses()
        self.assertEqual(found, [("WLAN", "192.168.1.3")])


class ChannelFilterSettingTest(unittest.TestCase):
    """按渠道过滤的开关：老配置没这几行时，界面必须显示「开」。"""

    FILTER_KEYS = (
        "notify.wecom_bot.events", "notify.wecom_bot.digest",
        "notify.wxpusher.events", "notify.wxpusher.digest",
        "notify.serverchan.events", "notify.serverchan.digest",
        "notify.webhook.events", "notify.webhook.digest",
    )

    def test_missing_keys_default_to_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        for key in self.FILTER_KEYS:
            self.assertIs(snap["values"][key], True, key)

    def test_filter_round_trips_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {
                "notify.wxpusher.events": "false",
                "notify.wecom_bot.digest": False,
            })
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
            snap = settings.snapshot(root)
        self.assertIs(data["notify"]["wxpusher"]["events"], False)
        self.assertIs(data["notify"]["wecom_bot"]["digest"], False)
        self.assertIs(snap["values"]["notify.wxpusher.events"], False)
        self.assertIs(snap["values"]["notify.wecom_bot.digest"], False)


    def test_events_deliverable_detects_all_channels_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            settings.apply(root, {
                "notify.channels": "wecom_bot,wxpusher",
                "notify.wecom_bot.events": False,
                "notify.wxpusher.events": False,
            })
            snap = settings.snapshot(root)
        self.assertFalse(snap["events_deliverable"])


class UpdateChannelSettingTest(unittest.TestCase):
    def test_defaults_to_stable_for_old_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            snap = settings.snapshot(root)
        self.assertEqual(snap["values"]["update.channel"], "stable")
        self.assertEqual(snap["values"]["update.check_interval_hours"], 24)

    def test_channel_round_trips_and_clears_update_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            state = root / "var" / "state"
            state.mkdir(parents=True)
            cache = state / "update.json"
            cache.write_text('{"latest": "0.1.10"}', encoding="utf-8")
            result = settings.apply(root, {"update.channel": "beta"})
            self.assertTrue(result["ok"], result)
            snap = settings.snapshot(root)
            local = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
        self.assertEqual(snap["values"]["update.channel"], "beta")
        self.assertEqual(local["update"]["channel"], "beta")
        self.assertFalse(cache.exists(), "切换通道后旧检查缓存应被清掉")

    def test_invalid_channel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"update.channel": "nightly"})
            self.assertFalse(result["ok"])
            self.assertIn("update.channel", result["errors"])


class HotReloadSettingTest(unittest.TestCase):
    def test_voice_change_writes_watcher_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {
                "voice.text": "欢迎回来",
                "voice.cooldown_seconds": 5,
            })
            marker = root / "var" / "state" / "reload.watcher"
            self.assertTrue(marker.is_file())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["requires_restart"], [])

    def test_guardian_change_writes_guardian_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"guardian.check_seconds": 30})
            marker = root / "var" / "state" / "reload.guardian"
            self.assertTrue(marker.is_file())
        self.assertEqual(result["requires_restart"], [])

    def test_update_change_needs_no_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"update.channel": "beta"})
        self.assertEqual(result["requires_restart"], [])

    def test_camera_change_still_requires_monitor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"camera.index": 1})
        self.assertEqual(result["requires_restart"], ["watcher"])

    def test_voice_volume_mode_still_requires_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"voice.volume_mode": "master"})
        self.assertEqual(result["requires_restart"], ["watcher"])


class RecordSettingsTest(unittest.TestCase):
    """录像策略与存储预算：默认值安全、跨字段校验、写 local 并复读。"""

    def test_record_fields_are_in_spec(self) -> None:
        for key in (
            "record.enabled",
            "record.min_seconds",
            "record.idle_stop_seconds",
            "record.max_seconds",
            "record.max_total_seconds",
            "record.owner_at_computer",
            "storage.max_total_gb",
            "storage.warn_days",
            "snapshot.keep_days",
        ):
            self.assertIn(key, settings.SPEC, key)
        self.assertEqual(settings.SPEC["record.owner_at_computer"].choices,
                         ("keep", "mute", "skip"))

    def test_invalid_owner_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {"record.owner_at_computer": "pause"})
            self.assertFalse(result["ok"])
            self.assertIn("record.owner_at_computer", result["errors"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_max_seconds_must_not_exceed_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {
                "record.max_seconds": 3600,
                "record.max_total_seconds": 1800,
            })
            self.assertFalse(result["ok"])
            self.assertIn("record.max_seconds", result["errors"])
            self.assertFalse((root / "config.local.yaml").exists())

    def test_min_seconds_must_not_exceed_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {
                "record.min_seconds": 300,
                "record.max_seconds": 120,
            })
            self.assertFalse(result["ok"])
            self.assertIn("record.min_seconds", result["errors"])

    def test_record_values_are_written_to_local_and_reread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = settings.apply(root, {
                "record.min_seconds": 10,
                "record.idle_stop_seconds": 15,
                "record.max_seconds": 120,
                "record.max_total_seconds": 600,
                "record.owner_at_computer": "skip",
                "storage.max_total_gb": 5,
                "storage.warn_days": 14,
            })
            self.assertTrue(result["ok"], result)
            data = yaml.safe_load((root / "config.local.yaml").read_text(encoding="utf-8"))
            from roomwatch.config import load_config

            cfg = load_config(str(root / "config.yaml"))
        self.assertEqual(data["record"]["owner_at_computer"], "skip")
        self.assertEqual(cfg["record"]["max_seconds"], 120)
        self.assertEqual(cfg["storage"]["warn_days"], 14)
        self.assertEqual(cfg["storage"]["max_total_gb"], 5)

    def test_snapshot_reports_storage_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp) / "data" / "captures"
            day = captures / "2026-10-18"
            day.mkdir(parents=True)
            (day / "120000.mp4").write_bytes(b"v" * 4096)
            # 用绝对路径：load_config 会按 PROJECT_ROOT 归一相对路径，
            # 临时项目里写相对路径会指到仓库自己的 data/ 去。
            root = make_project(Path(tmp), main_extra=(
                "record:\n"
                "  max_seconds: 120\n"
                "  max_total_seconds: 600\n"
                "  owner_at_computer: mute\n"
                "snapshot:\n"
                f"  dir: {captures.as_posix()}\n"
                "  keep_days: 30\n"
                "storage:\n"
                "  max_total_gb: 10\n"
                "  warn_days: 7\n"
            ))
            snap = settings.snapshot(root)
        storage = snap["storage"]
        self.assertIn("forecast", storage)
        self.assertEqual(storage["used_bytes"], 4096)
        self.assertEqual(storage["max_bytes"], 10 * 1024 ** 3)
        self.assertEqual(storage["keep_days"], 30)
        self.assertEqual(storage["forecast"]["used_bytes"], 4096)


if __name__ == "__main__":
    unittest.main()
