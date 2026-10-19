"""崩溃钩子与日志轮转的单测。

    python tests/test_diagnostics.py

不触发真实崩溃：只验证钩子的安装/恢复、退出记录的落盘与日志轮转。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import io
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from roomwatch import diagnostics, health, logutil


class RotatingHandlerTest(unittest.TestCase):
    def test_rotation_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.log"
            h = logutil.rotating_file_handler(p, max_bytes=200, backups=2)
            lg = logging.getLogger("rot-test")
            lg.setLevel(logging.INFO)
            lg.addHandler(h)
            lg.propagate = False
            for i in range(20):
                lg.info("line %d %s", i, "x" * 40)
            h.close()
            lg.removeHandler(h)
            self.assertTrue(p.exists())
            self.assertTrue((Path(tmp) / "x.log.1").exists())


class EventFormatTest(unittest.TestCase):
    def test_event_has_stable_key_value_format(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        lg = logging.getLogger("evt-test")
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.propagate = False
        logutil.event(lg, "camera_reopen", attempt=2)
        h.close()
        lg.removeHandler(h)
        out = buf.getvalue()
        self.assertIn("event=camera_reopen", out)
        self.assertIn("attempt=2", out)


class DiagnosticsTest(unittest.TestCase):
    def test_note_exit_writes_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.note_exit(health.EXIT_UNHANDLED, "unhandled")
            rec = health.read_exit_record(Path(tmp) / "last_exit.json")
            self.assertEqual((rec["code"], rec["reason"]), (5, "unhandled"))

    def test_last_note_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.note_exit(health.EXIT_OK, "normal")
            d.note_exit(health.EXIT_STALLED, "stalled")
            rec = health.read_exit_record(Path(tmp) / "last_exit.json")
            self.assertEqual((rec["code"], rec["reason"]), (6, "stalled"))

    def test_install_and_uninstall_restore_hooks(self):
        old_sys, old_thread = sys.excepthook, threading.excepthook
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.install()
            self.assertIsNot(sys.excepthook, old_sys)
            self.assertIsNot(threading.excepthook, old_thread)
            d.uninstall()
            self.assertIs(sys.excepthook, old_sys)
            self.assertIs(threading.excepthook, old_thread)

    def test_uninstall_without_install_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = diagnostics.Diagnostics(Path(tmp) / "fault.log", Path(tmp) / "last_exit.json")
            d.uninstall()


if __name__ == "__main__":
    unittest.main(verbosity=2)
