"""界面状态层的单测（不 import tkinter，任何解释器都能跑）。

    python tests/test_gui_state.py

界面最容易骗人的地方是「在线」：进程被强杀后 status.json 里留下的
stale 仍然是 false，`last_frame_at` 也还很新。0.1.7 的真实事故是——
停止后最多 120 秒里界面仍报在线，于是「启动监控」一直按不动。
所以判活必须**同时**看：心跳新鲜 + PID 还活着。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from roomwatch import control, gui

ALIVE = lambda pid: True      # noqa: E731 - 测试里的假判活
DEAD = lambda pid: False      # noqa: E731


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
            state = gui.read_state(Path(tmp), alive=ALIVE)
        self.assertFalse(state.online)
        self.assertIn("未运行", state.caption)
        self.assertEqual(state.phase, "stopped")

    def test_fresh_heartbeat_with_live_pid_is_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1)
            state = gui.read_state(root, alive=ALIVE)
        self.assertTrue(state.online)
        self.assertEqual(state.phase, "running")
        self.assertEqual(state.pid, 1234)
        self.assertEqual(state.events_today, 3)

    def test_fresh_heartbeat_but_dead_pid_is_offline(self) -> None:
        """这就是「停止后点不了启动」的根因，必须判离线。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1)
            state = gui.read_state(root, alive=DEAD)
        self.assertFalse(state.online)
        self.assertEqual(state.phase, "stopped")
        self.assertIn("已停止", state.caption)
        self.assertIsNone(state.pid, "死掉的 PID 不能再让预览/注册按钮变灰")

    def test_old_heartbeat_is_offline_even_if_file_says_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=600)
            state = gui.read_state(root, alive=ALIVE)
        self.assertFalse(state.online)
        self.assertGreater(state.stale_seconds, 120)

    def test_corrupt_status_is_offline_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "var" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "status.json").write_text("{not json", encoding="utf-8")
            state = gui.read_state(root, alive=ALIVE)
        self.assertFalse(state.online)
        self.assertIn("读不了", state.caption)

    def test_recording_flag_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1, recording=True)
            state = gui.read_state(root, alive=ALIVE)
        self.assertTrue(state.recording)

    def test_stop_request_puts_state_in_stopping_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1)
            control.request_stop(root, 1234)
            state = gui.read_state(root, alive=ALIVE)
        self.assertEqual(state.phase, "stopping")
        self.assertIn("正在停止", state.caption)
        self.assertTrue(state.online)

    def test_stale_stop_request_for_another_pid_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status(root, seconds_ago=1)
            control.request_stop(root, 999)
            state = gui.read_state(root, alive=ALIVE)
        self.assertEqual(state.phase, "running")


class ButtonStateTest(unittest.TestCase):
    def test_online_enables_stop_only(self) -> None:
        state = gui.MonitorState(online=True, pid=1, phase="running")
        self.assertEqual(gui.button_states(state), ("disabled", "normal"))

    def test_offline_enables_start_only(self) -> None:
        state = gui.MonitorState(online=False, phase="stopped")
        self.assertEqual(gui.button_states(state), ("normal", "disabled"))

    def test_busy_disables_both(self) -> None:
        """启动/停止进行中，两个按钮都锁住：不给「再点一次」的机会。"""
        for busy in ("starting", "stopping", "probe", "enroll"):
            state = gui.MonitorState(online=False, phase="stopped")
            self.assertEqual(gui.button_states(state, busy), ("disabled", "disabled"), busy)

    def test_busy_overrides_online_state(self) -> None:
        state = gui.MonitorState(online=True, pid=1, phase="stopping")
        self.assertEqual(gui.button_states(state, "stopping"), ("disabled", "disabled"))


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
        self.assertIsNone(events[0]["identity"], "旧库没有身份列时应回退为空")

    def test_identity_column_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "var" / "db"
            db_dir.mkdir(parents=True)
            conn = sqlite3.connect(db_dir / "events.db")
            conn.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                "snapshot TEXT, confidence REAL, notified INTEGER, video TEXT, identity TEXT)"
            )
            conn.execute(
                "INSERT INTO events (ts, snapshot, confidence, notified, video, identity) "
                "VALUES (?,?,?,?,?,?)",
                ("2026-10-18T03:00:00", "c.jpg", 0.8, 1, None, "other"),
            )
            conn.commit()
            conn.close()
            events = gui.read_events(root)
        self.assertEqual(events[0]["identity"], "other")

    def test_limit_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_db(root)
            self.assertEqual(len(gui.read_events(root, limit=1)), 1)


class IdentityLabelTest(unittest.TestCase):
    def test_known_labels(self) -> None:
        from roomwatch.labels import identity_text

        self.assertEqual(identity_text("owner"), "本人")
        self.assertEqual(identity_text("other"), "访客")
        self.assertEqual(identity_text("unknown"), "认不出")
        self.assertEqual(identity_text(None), "-")


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


class PreviewImageTest(unittest.TestCase):
    """预览图不能只看抓拍目录：probe 写的是 var/media/preview.jpg。"""

    def test_preview_wins_when_newer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cap = root / "data" / "captures" / "2026-10-19" / "031100.jpg"
            cap.parent.mkdir(parents=True)
            cap.write_bytes(b"x")
            preview = root / "var" / "media" / "preview.jpg"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"y")
            import os
            os.utime(cap, (1_700_000_000, 1_700_000_000))
            os.utime(preview, (1_700_000_600, 1_700_000_600))
            path, kind = gui.preview_image(root)
        self.assertEqual(path, preview)
        self.assertEqual(kind, "preview")

    def test_capture_wins_when_newer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cap = root / "data" / "captures" / "2026-10-19" / "031100.jpg"
            cap.parent.mkdir(parents=True)
            cap.write_bytes(b"x")
            preview = root / "var" / "media" / "preview.jpg"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"y")
            import os
            os.utime(preview, (1_700_000_000, 1_700_000_000))
            os.utime(cap, (1_700_000_600, 1_700_000_600))
            path, kind = gui.preview_image(root)
        self.assertEqual(path, cap)
        self.assertEqual(kind, "capture")

    def test_nothing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gui.preview_image(Path(tmp)), (None, ""))


class ControlTest(unittest.TestCase):
    def test_start_command_layout(self) -> None:
        cmd = gui.start_command("C:/py/python.exe", Path("C:/proj"), "C:/proj/config.yaml")
        self.assertEqual(cmd[:3], ["C:/py/python.exe", "-m", "roomwatch"])
        self.assertIn("start", cmd)
        self.assertIn("C:/proj/config.yaml", cmd)

    def test_force_kill_command_is_the_fallback_only(self) -> None:
        self.assertEqual(gui.force_kill_command(4321), ["taskkill", "/PID", "4321", "/F"])

    def test_probe_command_asks_for_json(self) -> None:
        cmd = gui.probe_command("C:/py/python.exe", "C:/proj/config.yaml")
        self.assertIn("probe", cmd)
        self.assertIn("--json", cmd)


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


class PausedStateTest(unittest.TestCase):
    def test_paused_and_offline_is_paused_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control.set_desired_state(root, "paused")
            state = gui.read_state(root, alive=DEAD)
        self.assertEqual(state.phase, "paused")
        self.assertIn("暂停", state.caption)

    def test_last_exit_reason_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/last_exit.json").write_text(
                '{"code": 5, "reason": "unhandled"}', encoding="utf-8")
            state = gui.read_state(root, alive=DEAD)
        self.assertIn("unhandled", state.caption)

    def test_selfcheck_failure_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/health.json").write_text(
                '{"selfcheck": [{"name": "推送", "ok": false, "detail": "未配置"}]}',
                encoding="utf-8")
            state = gui.read_state(root, alive=DEAD)
        self.assertIn("自检失败", state.caption)
        self.assertIn("推送", state.caption)


class GuardCommandTest(unittest.TestCase):
    def test_guard_command_layout(self):
        cmd = gui.guard_command("C:/py/python.exe", Path("C:/proj"), "C:/proj/config.yaml")
        self.assertIn("guard", cmd)
        self.assertIn("C:/proj/config.yaml", cmd)
        self.assertIn("--log", cmd)

    def test_guard_command_once(self):
        cmd = gui.guard_command("C:/py/python.exe", Path("C:/proj"), "C:/proj/config.yaml",
                                once=True)
        self.assertIn("--once", cmd)


class HeadlessSubprocessTest(unittest.TestCase):
    def test_headless_flags_on_non_windows(self):
        self.assertEqual(gui._headless_flags("linux"), 0)

    def test_headless_flags_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        self.assertTrue(gui._headless_flags("nt") & subprocess.CREATE_NO_WINDOW)

    def test_run_headless_passes_creationflags(self):
        with mock.patch.object(gui.subprocess, "run", return_value=mock.Mock()) as run:
            gui._run_headless(["python.exe", "-c", "pass"])
        self.assertEqual(
            run.call_args.kwargs.get("creationflags"), gui._headless_flags()
        )

    def test_update_check_uses_headless_run(self):
        with mock.patch.object(gui.subprocess, "run", return_value=mock.Mock()) as run:
            gui._update_check("py", Path("C:/proj"))
        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(args, ["py", "-m", "roomwatch", "update", "--json"])
        self.assertTrue(kwargs.get("capture_output"))
        self.assertEqual(kwargs.get("creationflags"), gui._headless_flags())


class GalleryUrlTest(unittest.TestCase):
    """相册端口不再写死：相册可能因系统保留端口而改用别的端口。"""

    def _write_state(self, root: Path, payload) -> None:
        state = root / "var" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "gallery.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_missing_state_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gui.gallery_url(Path(tmp)))

    def test_reads_actual_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_state(root, {
                "pid": 123, "host": "127.0.0.1", "port": 54321,
                "url": "http://127.0.0.1:54321/",
            })
            self.assertEqual(gui.gallery_url(root), "http://127.0.0.1:54321/")

    def test_builds_url_from_host_and_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_state(root, {"pid": 123, "host": "127.0.0.1", "port": 54321})
            self.assertEqual(gui.gallery_url(root), "http://127.0.0.1:54321/")

    def test_invalid_state_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_state(root, {"pid": 123, "port": "not-a-port"})
            self.assertIsNone(gui.gallery_url(root))
            self._write_state(root, {"pid": 123, "port": 70000})
            self.assertIsNone(gui.gallery_url(root))

    def test_zero_host_opens_via_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_state(root, {
                "pid": 123, "host": "0.0.0.0", "port": 18765,
                "url": "http://0.0.0.0:18765/",
            })
            self.assertEqual(gui.gallery_url(root), "http://127.0.0.1:18765/")


if __name__ == "__main__":
    unittest.main()
