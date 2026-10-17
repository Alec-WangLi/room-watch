"""桌面界面的状态层单测（不 import tkinter，任何解释器都能跑）。

    python tests/test_gui_state.py

界面最容易骗人的地方是「在线」：进程被强杀后 status.json 里留下的
stale 仍然是 false。所以判活必须在**读取的那一刻**重算，
这里就是在钉住这个行为。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from roomwatch import gui


def write_status(root: Path, seconds_ago: float, **extra) -> None:
    state_dir = root / "var" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    frame_at = datetime.now() - timedelta(seconds=seconds_ago)
    payload = {
        "pid": 1234,
        "started_at": frame_at.isoformat(timespec="seconds"),
        "last_frame_at": frame_at.isoformat(timespec="seconds"),
        "stale": False,          # 故意写 false：界面不许信它
        "camera_ok": True,
        "frames": 100,
        "events_today": 3,
        "recording": False,
    }
    payload.update(extra)
    (state_dir / "status.json").write_text(json.dumps(payload), encoding="utf-8")


class ReadStateTest(unittest.TestCase):
    def test_missing_status_means_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = gui.read_state(Path(tmp))
        self.assertFalse(state.online)
        self.assertIn("未运行", state.caption)

    def test_fresh_heartbeat_is_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1)
            state = gui.read_state(root)
        self.assertTrue(state.online)
        self.assertEqual(state.pid, 1234)
        self.assertEqual(state.events_today, 3)

    def test_old_heartbeat_is_offline_even_if_file_says_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=600)
            state = gui.read_state(root)
        self.assertFalse(state.online)
        self.assertGreater(state.stale_seconds, 120)

    def test_corrupt_status_is_offline_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "var" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "status.json").write_text("{not json", encoding="utf-8")
            state = gui.read_state(root)
        self.assertFalse(state.online)
        self.assertIn("读不了", state.caption)

    def test_recording_flag_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1, recording=True)
            state = gui.read_state(root)
        self.assertTrue(state.recording)


class ReadEventsTest(unittest.TestCase):
    def make_db(self, root: Path) -> None:
        db_dir = root / "var" / "db"
        db_dir.mkdir(parents=True)
        conn = sqlite3.connect(db_dir / "events.db")
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "snapshot TEXT, confidence REAL, notified INTEGER, video TEXT)"
        )
        conn.executemany(
            "INSERT INTO events (ts, snapshot, confidence, notified, video) VALUES (?,?,?,?,?)",
            [("2026-10-18T01:00:00", "a.jpg", 0.91, 1, "a.mp4"),
             ("2026-10-18T02:00:00", "b.jpg", 0.77, 0, None)],
        )
        conn.commit()
        conn.close()

    def test_missing_db_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gui.read_events(Path(tmp)), [])

    def test_events_are_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_db(root)
            events = gui.read_events(root)
        self.assertEqual([e["ts"] for e in events], ["2026-10-18T02:00:00", "2026-10-18T01:00:00"])
        self.assertEqual(events[0]["confidence"], 0.77)

    def test_limit_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_db(root)
            self.assertEqual(len(gui.read_events(root, limit=1)), 1)


class LatestSnapshotTest(unittest.TestCase):
    def test_picks_newest_file_across_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "data" / "captures" / "2026-10-17"
            new = root / "data" / "captures" / "2026-10-18"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (old / "235959.jpg").write_bytes(b"x")
            newest = new / "000001.jpg"
            newest.write_bytes(b"x")
            self.assertEqual(gui.latest_snapshot(root), newest)

    def test_no_snapshot_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gui.latest_snapshot(Path(tmp)))


class ControlTest(unittest.TestCase):
    def test_start_command_layout(self) -> None:
        cmd = gui.start_command("C:/py/python.exe", Path("C:/proj"), "C:/proj/config.yaml")
        self.assertEqual(cmd[:3], ["C:/py/python.exe", "-m", "roomwatch"])
        self.assertIn("start", cmd)
        self.assertIn("C:/proj/config.yaml", cmd)

    def test_stop_command_uses_taskkill(self) -> None:
        self.assertEqual(gui.stop_command(4321), ["taskkill", "/PID", "4321", "/F"])


class ParseArgsTest(unittest.TestCase):
    """exe 入口是无参调用，必须回落到 sys.argv（曾经因此丢掉 --selftest）。"""

    def test_none_reads_sys_argv(self) -> None:
        with mock.patch.object(sys, "argv", ["room-watch.exe", "--selftest"]):
            args, root = gui.parse_args(None)
        self.assertEqual(args, ["--selftest"])
        self.assertIsNone(root)

    def test_explicit_argv_is_used(self) -> None:
        args, root = gui.parse_args(["--selftest", "--root", "C:/proj"])
        self.assertEqual(args, ["--selftest"])
        self.assertEqual(root, Path("C:/proj").resolve())

    def test_dangling_root_is_ignored(self) -> None:
        args, root = gui.parse_args(["--root"])
        self.assertEqual(args, [])
        self.assertIsNone(root)


if __name__ == "__main__":
    unittest.main()
