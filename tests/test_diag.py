"""诊断包的单测：脱敏与打包内容。

    python tests/test_diag.py

打包本身不联网、不读摄像头；只验证「该带的带上、密钥不留原值、data/ 不进去」。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tempfile
import unittest
import zipfile
from pathlib import Path

from roomwatch import diag


class MaskTest(unittest.TestCase):
    def test_masks_secret_values_but_keeps_tail(self):
        cfg = {
            "notify": {
                "wecom_bot": {"webhook": "https://qyapi.weixin.qq.com/send?key=abcdef123456"},
                "wxpusher": {"app_token": "AT_123456", "uid": "UID_9999"},
            }
        }
        masked = diag.mask_config(cfg)
        self.assertIn("3456", masked["notify"]["wecom_bot"]["webhook"])
        self.assertNotIn("abcdef", masked["notify"]["wecom_bot"]["webhook"])
        self.assertNotIn("123456", masked["notify"]["wxpusher"]["app_token"])
        self.assertNotIn("UID_9999", masked["notify"]["wxpusher"]["uid"])

    def test_plain_values_unchanged(self):
        self.assertEqual(diag.mask_config({"camera": {"index": 0}}), {"camera": {"index": 0}})

    def test_short_values_are_fully_masked(self):
        self.assertEqual(diag.mask_value("abc"), "***")

    def test_nested_lists_are_copied(self):
        data = {"notify": {"channels": ["wecom_bot"]}}
        self.assertEqual(diag.mask_config(data), data)


class BundleTest(unittest.TestCase):
    def test_bundle_contains_masked_local_config_and_no_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var" / "logs").mkdir(parents=True)
            (root / "var" / "logs" / "watcher.log").write_text("hello", encoding="utf-8")
            (root / "var" / "state").mkdir(parents=True)
            (root / "var" / "state" / "health.json").write_text("{}", encoding="utf-8")
            (root / "data" / "captures").mkdir(parents=True)
            (root / "data" / "captures" / "x.jpg").write_bytes(b"private")
            (root / "config.local.yaml").write_text(
                "notify:\n  wecom_bot:\n    webhook: https://x/send?key=SECRET1234\n",
                encoding="utf-8",
            )
            out = diag.build_bundle(root)
            self.assertTrue(out.is_file())
            self.assertEqual(out.parent, root / "var" / "diag")
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("var/logs/watcher.log", names)
                self.assertIn("var/state/health.json", names)
                self.assertIn("config.local.yaml", names)
                self.assertIn("system.txt", names)
                text = zf.read("config.local.yaml").decode("utf-8")
                self.assertNotIn("SECRET1234", text)
                self.assertFalse(any(n.startswith("data/") for n in names))

    def test_missing_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = diag.build_bundle(Path(tmp))
            with zipfile.ZipFile(out) as zf:
                self.assertIn("system.txt", zf.namelist())

    def test_rotated_logs_are_included(self):
        """RotatingFileHandler 的备份名是 watcher.log.1，*.log 匹配不到。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "var" / "logs"
            logs.mkdir(parents=True)
            (logs / "watcher.log").write_text("current", encoding="utf-8")
            (logs / "watcher.log.1").write_text("previous", encoding="utf-8")
            (logs / "watcher.log.2").write_text("older", encoding="utf-8")
            out = diag.build_bundle(root)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
        self.assertIn("var/logs/watcher.log.1", names)
        self.assertIn("var/logs/watcher.log.2", names)

    def test_recent_events_and_structured_log_events_are_summarized(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "var" / "logs"
            logs.mkdir(parents=True)
            (logs / "watcher.log").write_text(
                "12:00:00 INFO 2026-10-19 12:00:00 event=watcher_exit code=0 reason=keyboard\n",
                encoding="utf-8",
            )
            db_dir = root / "var" / "db"
            db_dir.mkdir(parents=True)
            conn = sqlite3.connect(db_dir / "events.db")
            conn.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                "snapshot TEXT NOT NULL, confidence REAL, notified INTEGER NOT NULL DEFAULT 0, "
                "video TEXT, identity TEXT)"
            )
            conn.execute(
                "INSERT INTO events (ts, snapshot, confidence, notified, identity) "
                "VALUES ('2026-10-19T12:00:00', 'data/captures/x.jpg', 0.9, 1, 'other')"
            )
            conn.commit()
            conn.close()

            out = diag.build_bundle(root)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                events_text = zf.read("events.txt").decode("utf-8")
                log_text = zf.read("log_events.txt").decode("utf-8")

        self.assertIn("events.txt", names)
        self.assertIn("2026-10-19T12:00:00", events_text)
        self.assertIn("other", events_text)
        self.assertIn("watcher_exit", log_text)
        self.assertNotIn("data/captures", events_text)  # 不把隐私路径带进诊断包


if __name__ == "__main__":
    unittest.main(verbosity=2)
