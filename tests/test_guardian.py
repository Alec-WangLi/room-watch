"""守护进程判据测试。

守护逻辑最怕两类错：
1. **该救没救** —— 进程死了却判成「正常」，用户在外面一无所知
2. **不该杀乱杀** —— 把正在自愈的进程掐掉，反而制造故障
所以每个分支都单独钉住，尤其是「卡死」的连续确认机制。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from roomwatch import control
from roomwatch import guardian as G


LIVE_PID = os.getpid()
DEAD_PID = 999_999_999


def status_with(pid, age_seconds=None, raw_time=None):
    s = {"pid": pid}
    if raw_time is not None:
        s["last_frame_at"] = raw_time
    elif age_seconds is not None:
        s["last_frame_at"] = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    return s


class TestPidAlive(unittest.TestCase):
    def test_none_is_false(self):
        self.assertFalse(G.pid_alive(None))

    def test_zero_is_false(self):
        self.assertFalse(G.pid_alive(0))

    def test_negative_is_false(self):
        self.assertFalse(G.pid_alive(-1))

    def test_self_is_true(self):
        self.assertTrue(G.pid_alive(LIVE_PID))

    def test_bogus_pid_is_false(self):
        self.assertFalse(G.pid_alive(DEAD_PID))


class TestFrameAge(unittest.TestCase):
    def test_none_status(self):
        self.assertIsNone(G.frame_age_seconds(None))

    def test_missing_field(self):
        self.assertIsNone(G.frame_age_seconds({"pid": 1}))

    def test_bad_format(self):
        self.assertIsNone(G.frame_age_seconds({"last_frame_at": "not-a-time"}))

    def test_positive_age(self):
        age = G.frame_age_seconds(status_with(LIVE_PID, age_seconds=42))
        self.assertAlmostEqual(age, 42, delta=2)


class TestDecide(unittest.TestCase):
    def test_no_status_waits(self):
        """状态文件都没有，不能凭它下结论。"""
        self.assertEqual(G.decide(None, datetime.now(), 300, 0, 2), G.ACT_WAIT)

    def test_dead_pid_restarts_immediately(self):
        """进程没了要立刻拉起，不需要连续确认。"""
        s = status_with(DEAD_PID, age_seconds=0)
        self.assertEqual(G.decide(s, datetime.now(), 300, 0, 2), G.ACT_RESTART_GONE)

    def test_fresh_heartbeat_is_ok(self):
        s = status_with(LIVE_PID, age_seconds=5)
        self.assertEqual(G.decide(s, datetime.now(), 300, 0, 2), G.ACT_OK)

    def test_stale_needs_confirmation_first(self):
        """第一次发现卡死先观察——可能是摄像头正在重连。"""
        s = status_with(LIVE_PID, age_seconds=600)
        self.assertEqual(G.decide(s, datetime.now(), 300, 0, 2), G.ACT_WAIT)

    def test_stale_restarts_after_confirmation(self):
        s = status_with(LIVE_PID, age_seconds=600)
        self.assertEqual(G.decide(s, datetime.now(), 300, 1, 2), G.ACT_RESTART_STALLED)

    def test_single_confirmation_config(self):
        """确认次数配成 1 时就该直接动手。"""
        s = status_with(LIVE_PID, age_seconds=600)
        self.assertEqual(G.decide(s, datetime.now(), 300, 0, 1), G.ACT_RESTART_STALLED)

    def test_bad_timestamp_treated_as_stalled(self):
        s = {"pid": LIVE_PID, "last_frame_at": "garbage"}
        self.assertEqual(G.decide(s, datetime.now(), 300, 5, 2), G.ACT_RESTART_STALLED)

    def test_threshold_boundary_is_ok(self):
        """恰好等于阈值仍算正常（<=）。用固定时间点，避免 now() 漂移把边界测穿。"""
        now = datetime(2026, 10, 17, 23, 0, 0)
        s = {"pid": LIVE_PID, "last_frame_at": (now - timedelta(seconds=300)).isoformat()}
        self.assertEqual(G.decide(s, now, 300, 0, 2), G.ACT_OK)


class TestBackoff(unittest.TestCase):
    def test_zero_failures_no_wait(self):
        self.assertEqual(G.backoff_seconds(0), 0)

    def test_first_failure_waits_a_minute(self):
        self.assertEqual(G.backoff_seconds(1), 60)

    def test_doubles(self):
        self.assertEqual(G.backoff_seconds(2), 120)
        self.assertEqual(G.backoff_seconds(3), 240)
        self.assertEqual(G.backoff_seconds(4), 480)

    def test_capped(self):
        self.assertEqual(G.backoff_seconds(10, cap=900), 900)

    def test_custom_cap(self):
        self.assertEqual(G.backoff_seconds(5, cap=100), 100)


class TestFileLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "g.lock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_acquire_and_release(self):
        lock = G.FileLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertTrue(os.path.exists(self.path))
        lock.release()
        self.assertFalse(os.path.exists(self.path))

    def test_second_instance_blocked(self):
        a = G.FileLock(self.path)
        b = G.FileLock(self.path)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_stale_lock_from_dead_process_is_taken_over(self):
        """上一个守护崩了会留锁，不清掉的话守护永久失效——这是锁文件最大的坑。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(str(DEAD_PID))
        lock = G.FileLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_garbage_lock_is_taken_over(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not-a-pid")
        lock = G.FileLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_context_manager_releases(self):
        with G.FileLock(self.path) as got:
            self.assertTrue(got)
        self.assertFalse(os.path.exists(self.path))


class TestReadStatus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_file(self):
        self.assertIsNone(G.read_status(os.path.join(self.dir, "nope.json")))

    def test_broken_json(self):
        p = os.path.join(self.dir, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(G.read_status(p))

    def test_valid(self):
        p = os.path.join(self.dir, "ok.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"pid": 123, "last_frame_at": "2026-10-17T23:00:00"}')
        self.assertEqual(G.read_status(p)["pid"], 123)

    def test_non_dict_json(self):
        p = os.path.join(self.dir, "list.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")
        self.assertIsNone(G.read_status(p))


class HistoryTest(unittest.TestCase):
    def test_append_and_read_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "h.jsonl"
            G.append_history(p, {"service": "watcher", "action": "restart_gone"})
            G.append_history(p, {"service": "gallery", "action": "restart_gone"})
            rows = G.read_history(p, limit=10)
            self.assertEqual([r["service"] for r in rows], ["watcher", "gallery"])

    def test_read_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "h.jsonl"
            self.assertEqual(G.read_history(p), [])
            p.write_text("{oops\n\n[]\n", encoding="utf-8")
            self.assertEqual(G.read_history(p), [])

    def test_missing_exit_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = G.Guard({"guardian": {}, "storage": {"status": str(Path(tmp) / "s.json")},
                         "health": {"exit_record": str(Path(tmp) / "last_exit.json")},
                         "__config_path__": str(Path(tmp) / "c.yaml")}, dry_run=True)
            self.assertEqual(g.last_exit_reason(), "unknown")

    def test_corrupt_exit_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            p.write_text("{oops", encoding="utf-8")
            g = G.Guard({"guardian": {}, "storage": {"status": str(Path(tmp) / "s.json")},
                         "health": {"exit_record": str(p)},
                         "__config_path__": str(Path(tmp) / "c.yaml")}, dry_run=True)
            self.assertEqual(g.last_exit_reason(), "unknown")

    def test_exit_reason_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            p.write_text(json.dumps({"code": 5, "reason": "unhandled"}), encoding="utf-8")
            g = G.Guard({"guardian": {}, "storage": {"status": str(Path(tmp) / "s.json")},
                         "health": {"exit_record": str(p)},
                         "__config_path__": str(Path(tmp) / "c.yaml")}, dry_run=True)
            self.assertEqual(g.last_exit_reason(), "unhandled")


class GalleryDecideTest(unittest.TestCase):
    def test_dead_pid_restarts(self):
        self.assertEqual(G.decide_gallery(None, lambda p: False), G.ACT_RESTART_GONE)
        self.assertEqual(G.decide_gallery(123, lambda p: False), G.ACT_RESTART_GONE)

    def test_alive_matching_pid_is_ok(self):
        cmd = ["python", "-m", "roomwatch", "gallery"]
        self.assertEqual(G.decide_gallery(123, lambda p: True,
                                          cmdline=lambda p: cmd), G.ACT_OK)

    def test_reused_pid_is_restart(self):
        self.assertEqual(G.decide_gallery(123, lambda p: True,
                                          cmdline=lambda p: ["notepad.exe"]),
                         G.ACT_RESTART_GONE)


class CrashWindowTest(unittest.TestCase):
    def test_counts_only_inside_window(self):
        now = datetime(2026, 10, 19, 12, 0, 0)
        stamps = [(now - timedelta(minutes=m)).isoformat() for m in (1, 5, 15)]
        self.assertEqual(G.crash_window_count(stamps, now, 10), 2)

    def test_bad_timestamps_are_skipped(self):
        now = datetime(2026, 10, 19, 12, 0, 0)
        self.assertEqual(G.crash_window_count(["garbage", None], now, 10), 0)

    def test_future_timestamp_is_skipped(self):
        now = datetime(2026, 10, 19, 12, 0, 0)
        future = (now + timedelta(minutes=5)).isoformat()
        self.assertEqual(G.crash_window_count([future], now, 10), 0)


class TextTest(unittest.TestCase):
    def test_recovery_mentions_reason(self):
        text = G.recovery_text("watcher", {"reason": "unhandled", "code": 5})
        self.assertIn("watcher", text)
        self.assertIn("unhandled", text)

    def test_crash_alert_mentions_count(self):
        text = G.crash_alert_text("watcher", 3, 10, {"reason": "unhandled"})
        self.assertIn("3", text)
        self.assertIn("10", text)


class GuardPausedTest(unittest.TestCase):
    def test_paused_does_not_start_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"guardian": {
                       "desired_state": str(root / "var/state/desired_state.json"),
                       "state": str(root / "var/state/guardian_state.json"),
                       "lock": str(root / "var/state/guardian.lock"),
                       "history": str(root / "var/state/h.jsonl"),
                       "supervise_gallery": False},
                   "health": {"exit_record": str(root / "var/state/last_exit.json")},
                   "storage": {"status": str(root / "var/state/status.json")},
                   "__config_path__": str(root / "config.yaml")}
            control.set_desired_state(root, "paused")
            g = G.Guard(cfg, dry_run=True)
            g._notify = lambda *args, **kwargs: None
            with mock.patch.object(g, "_start_watcher") as sw, \
                 mock.patch.object(g, "_start_gallery") as sg:
                self.assertEqual(g.check_once(), G.ACT_PAUSED)
                sw.assert_not_called()
                sg.assert_not_called()

    def test_pause_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"guardian": {
                       "desired_state": str(root / "var/state/desired_state.json"),
                       "state": str(root / "var/state/guardian_state.json"),
                       "lock": str(root / "var/state/guardian.lock"),
                       "history": str(root / "var/state/h.jsonl"),
                       "supervise_gallery": False},
                   "health": {"exit_record": str(root / "var/state/last_exit.json")},
                   "storage": {"status": str(root / "var/state/status.json")},
                   "__config_path__": str(root / "config.yaml")}
            control.set_desired_state(root, "paused")
            g = G.Guard(cfg, dry_run=True)
            sent = []
            g._notify = lambda text: sent.append(text)
            self.assertEqual(g.check_once(), G.ACT_PAUSED)
            self.assertEqual(len(sent), 1)
            self.assertIn("暂停", sent[0])
            g.check_once()
            self.assertEqual(len(sent), 1)


    def test_paused_clears_pending_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"guardian": {
                       "desired_state": str(root / "var/state/desired_state.json"),
                       "state": str(root / "var/state/guardian_state.json"),
                       "lock": str(root / "var/state/guardian.lock"),
                       "history": str(root / "var/state/h.jsonl"),
                       "supervise_gallery": False},
                   "health": {"exit_record": str(root / "var/state/last_exit.json")},
                   "storage": {"status": str(root / "var/state/status.json")},
                   "__config_path__": str(root / "config.yaml")}
            control.set_desired_state(root, "paused")
            g = G.Guard(cfg, dry_run=True)
            g._notify = lambda *args, **kwargs: None
            g.state["pending_recovery"] = {"pid": 123, "at": "2026-10-19T12:00:00"}
            with mock.patch.object(g, "_start_watcher"), \
                 mock.patch.object(g, "_start_gallery"):
                self.assertEqual(g.check_once(), G.ACT_PAUSED)
            self.assertIsNone(g.state.get("pending_recovery"),
                              "暂停后不该保留「待恢复」状态")


class GuardRecoveryTest(unittest.TestCase):
    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "gallery_pid": str(root / "var/state/gallery.pid"),
                    "gallery_log": str(root / "var/logs/gallery.log"),
                    "supervise_gallery": False},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_guardian_reload_updates_check_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            cfg["__config_path__"] = str(root / "config.yaml")
            (root / "config.yaml").write_text(
                "guardian:\n  check_seconds: 30\n", encoding="utf-8")
            g = G.Guard(cfg, dry_run=True)
            state = root / "var" / "state"
            state.mkdir(parents=True, exist_ok=True)
            marker = state / "reload.guardian"
            marker.write_text("guardian", encoding="utf-8")
            g.maybe_reload()
            self.assertFalse(marker.exists())
            self.assertEqual(g.check_seconds, 30.0)

    def test_recovery_notification_sent_once_after_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["pending_recovery"] = {"service": "watcher", "reason": "unhandled"}
            sent = []
            g._notify = lambda text: sent.append(text)
            self.assertEqual(g.check_once(), G.ACT_OK)
            self.assertEqual(len(sent), 1)
            self.assertIn("unhandled", sent[0])
            g.check_once()
            self.assertEqual(len(sent), 1)

    def test_stalled_restart_uses_graceful_stop_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": (datetime.now() - timedelta(seconds=600))
                .isoformat(timespec="seconds")}), encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            g._notify = lambda *args, **kwargs: None
            g.state["stall_streak"] = max(0, g.stall_confirmations - 1)
            with mock.patch("roomwatch.guardian.control.stop_watcher",
                            return_value={"method": "forced"}) as stop, \
                 mock.patch.object(g, "_start_watcher", return_value=4242), \
                 mock.patch.object(g, "_kill") as kill:
                action = g.check_once()
        self.assertEqual(action, G.ACT_RESTART_STALLED)
        stop.assert_called_once()
        kill.assert_not_called()

    def test_selfcheck_failure_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            (root / "var/state/health.json").write_text(json.dumps({
                "selfcheck": [{"name": "推送", "ok": False, "detail": "未配置"}]}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            sent = []
            g._notify = lambda text: sent.append(text)
            g.check_once()
            self.assertTrue(any("自检" in s for s in sent))


class GuardStateRefreshTest(unittest.TestCase):
    """`rw start` 会同时跑常驻 guardian 和 `guard --once`。

    两个进程各自构造 Guard；`--once` 若沿用构造时的旧状态，就看不到常驻
    guardian 刚拉起的 watcher，会再拉起一个（真机 0.1.22 升级时实测两个 watcher
    抢同一个摄像头）。拿到检查锁后必须重新读状态。
    """

    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "gallery_pid": str(root / "var/state/gallery.pid"),
                    "gallery_log": str(root / "var/logs/gallery.log"),
                    "supervise_gallery": False},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_once_sees_spawn_recorded_by_resident_guardian(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "var" / "state"
            state.mkdir(parents=True)
            (state / "status.json").write_text(json.dumps({
                "pid": DEAD_PID,
                "last_frame_at": (datetime.now() - timedelta(minutes=5))
                .isoformat(timespec="seconds"),
            }), encoding="utf-8")

            resident = G.Guard(self._cfg(root), dry_run=True)
            once = G.Guard(self._cfg(root), dry_run=True)   # 先构造，拿到的是旧状态
            resident._notify = lambda *args, **kwargs: None
            once._notify = lambda *args, **kwargs: None

            with mock.patch.object(resident, "_start_watcher", return_value=LIVE_PID):
                self.assertEqual(resident.check_once(), G.ACT_RESTART_GONE)

            # 拿到检查锁后的刷新动作；没有这一步，once 会再次拉起 watcher。
            once._reload_state()
            with mock.patch.object(once, "_start_watcher", return_value=4242) as start:
                self.assertEqual(once.check_once(), G.ACT_WAIT)
            start.assert_not_called()


class GalleryHealthTest(unittest.TestCase):
    """相册进程活着但 HTTP 卡死时，guardian 要能发现并重启。"""

    def test_http_ok_against_real_server(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - 标准库命名
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(G.gallery_http_ok(server.server_address[1], timeout=2.0))
        finally:
            server.shutdown()
            server.server_close()

    def test_closed_port_is_not_ok(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(G.gallery_http_ok(port, timeout=0.3))

    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "gallery_pid": str(root / "var/state/gallery.pid"),
                    "gallery_log": str(root / "var/logs/gallery.log"),
                    "supervise_gallery": True},
                "gallery": {"port": 8765, "host": "127.0.0.1"},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def _guard(self, root):
        state = root / "var" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "gallery.pid").write_text(str(LIVE_PID), encoding="utf-8")
        g = G.Guard(self._cfg(root), dry_run=True)
        g._notify = lambda *args, **kwargs: None
        return g

    def test_two_http_failures_restart_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g = self._guard(root)
            now = datetime.now()
            with mock.patch.object(G, "decide_gallery", return_value=G.ACT_OK), \
                 mock.patch.object(G, "gallery_http_ok", return_value=False), \
                 mock.patch("roomwatch.guardian.control.stop_gallery",
                            return_value={"method": "forced"}) as stop, \
                 mock.patch.object(g, "_start_gallery", return_value=4242) as start:
                self.assertEqual(g._check_gallery(now), G.ACT_WAIT)
                self.assertEqual(g._check_gallery(now), G.ACT_RESTART_STALLED)
        stop.assert_called_once()
        start.assert_called_once()
        self.assertEqual(g.state.get("gallery_http_failures"), 0)

    def test_http_ok_clears_failure_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g = self._guard(root)
            g.state["gallery_http_failures"] = 1
            with mock.patch.object(G, "decide_gallery", return_value=G.ACT_OK), \
                 mock.patch.object(G, "gallery_http_ok", return_value=True):
                self.assertEqual(g._check_gallery(datetime.now()), G.ACT_OK)
        self.assertEqual(g.state.get("gallery_http_failures"), 0)

    def test_uses_actual_port_from_gallery_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g = self._guard(root)
            (root / "var" / "state" / "gallery.json").write_text(
                json.dumps({"pid": LIVE_PID, "port": 9999}), encoding="utf-8")
            with mock.patch.object(G, "decide_gallery", return_value=G.ACT_OK), \
                 mock.patch.object(G, "gallery_http_ok", return_value=True) as probe:
                g._check_gallery(datetime.now())
        probe.assert_called_once_with(9999, timeout=3.0)


class GuardSupervisionTest(unittest.TestCase):
    def _cfg(self, root, **guardian):
        base = {
            "desired_state": str(root / "var/state/desired_state.json"),
            "state": str(root / "var/state/guardian_state.json"),
            "lock": str(root / "var/state/guardian.lock"),
            "history": str(root / "var/state/h.jsonl"),
            "gallery_pid": str(root / "var/state/gallery.pid"),
            "gallery_log": str(root / "var/logs/gallery.log"),
            "supervise_gallery": False,
        }
        base.update(guardian)
        return {"guardian": base,
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_missing_gallery_is_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": os.getpid(),
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root, supervise_gallery=True), dry_run=True)
            started = []
            g._start_gallery = lambda: started.append(True) or 999
            self.assertEqual(g.check_once(), G.ACT_OK)
            self.assertTrue(started)

    def test_crash_threshold_sends_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": 999_999_999,
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root, crash_threshold=2, crash_window_minutes=10),
                        dry_run=True)
            g._start_watcher = lambda: 123
            g.state["restart_times"] = [
                (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
            ]
            sent = []
            g._notify = lambda text: sent.append(text)
            self.assertEqual(g.check_once(), G.ACT_RESTART_GONE)
            self.assertTrue(any("重启" in text for text in sent))


class WatcherStartTest(unittest.TestCase):
    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "supervise_gallery": False},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_watcher_command_is_foreground(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = G.Guard(self._cfg(Path(tmp)), dry_run=True)
            cmd = g._watcher_cmd()
        self.assertIn("start", cmd)
        self.assertIn("--foreground", cmd)
        self.assertIn("roomwatch", cmd)

    def test_first_run_starts_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            g = G.Guard(self._cfg(root), dry_run=True)
            g._start_watcher = lambda: 123
            self.assertEqual(g.check_once(), G.ACT_STARTED)
            self.assertEqual(g.state["last_spawn_pid"], 123)

    def test_recent_spawn_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["last_spawn_pid"] = os.getpid()
            g.state["last_spawn_at"] = datetime.now().isoformat(timespec="seconds")
            calls = []
            g._start_watcher = lambda: calls.append(True) or 123
            self.assertEqual(g.check_once(), G.ACT_WAIT)
            self.assertEqual(calls, [])


class ConfigMergeTest(unittest.TestCase):
    def test_load_config_merges_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "config.yaml"
            main.write_text("notify:\n  channel: none\n", encoding="utf-8")
            (root / "config.local.yaml").write_text(
                "notify:\n  channel: wxpusher\n  wxpusher:\n"
                "    app_token: AT_REAL\n    uid: UID_REAL\n",
                encoding="utf-8",
            )
            cfg = G.load_config(str(main))
        self.assertEqual(cfg["notify"]["channel"], "wxpusher")
        self.assertEqual(cfg["notify"]["wxpusher"]["app_token"], "AT_REAL")


class ExitAttributionTest(unittest.TestCase):
    def _cfg(self, root):
        return {"guardian": {
                    "desired_state": str(root / "var/state/desired_state.json"),
                    "state": str(root / "var/state/guardian_state.json"),
                    "lock": str(root / "var/state/guardian.lock"),
                    "history": str(root / "var/state/h.jsonl"),
                    "supervise_gallery": False},
                "health": {"path": str(root / "var/state/health.json"),
                           "exit_record": str(root / "var/state/last_exit.json")},
                "storage": {"status": str(root / "var/state/status.json")},
                "__config_path__": str(root / "config.yaml")}

    def test_stale_exit_record_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": 999_999_998,
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            (root / "var/state/last_exit.json").write_text(json.dumps({
                "pid": 999_999_999, "code": 0, "reason": "normal"}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            g._start_watcher = lambda: 123
            g.check_once()
            rows = G.read_history(root / "var/state/h.jsonl")
        self.assertEqual(rows[-1]["exit_reason"], "unknown")

    def test_matching_exit_record_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": 999_999_998,
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            (root / "var/state/last_exit.json").write_text(json.dumps({
                "pid": 999_999_998, "code": 5, "reason": "unhandled"}),
                encoding="utf-8")
            g = G.Guard(self._cfg(root), dry_run=True)
            g._start_watcher = lambda: 123
            g.check_once()
            rows = G.read_history(root / "var/state/h.jsonl")
        self.assertEqual(rows[-1]["exit_reason"], "unhandled")


class RestartBackoffTest(unittest.TestCase):
    def test_recent_restart_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": 999_999_998,
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            cfg = ExitAttributionTest()._cfg(root)
            g = G.Guard(cfg, dry_run=True)
            g.state["restart_times"] = [
                (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
            ]
            g.state["last_restart_at"] = g.state["restart_times"][0]
            start = mock.Mock(return_value=123)
            g._start_watcher = start
            self.assertEqual(g.check_once(), G.ACT_BACKOFF)
            start.assert_not_called()


class HistoryDetailTest(unittest.TestCase):
    def test_history_has_uptime_and_health_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            started = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            (root / "var/state/status.json").write_text(json.dumps({
                "pid": 999_999_998, "started_at": started,
                "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            (root / "var/state/health.json").write_text(json.dumps({
                "loop_seq": 42, "consecutive_errors": 3,
                "camera_reopens": 1, "rss_mb": 120.5}), encoding="utf-8")
            cfg = ExitAttributionTest()._cfg(root)
            g = G.Guard(cfg, dry_run=True)
            g._start_watcher = lambda: 123
            g.check_once()
            rows = G.read_history(root / "var/state/h.jsonl")
        self.assertGreaterEqual(rows[-1]["uptime_seconds"], 3500)
        self.assertEqual(rows[-1]["health"]["loop_seq"], 42)


class GalleryBackoffTest(unittest.TestCase):
    def _cfg(self, root):
        cfg = ExitAttributionTest()._cfg(root)
        cfg["guardian"]["supervise_gallery"] = True
        cfg["guardian"]["gallery_pid"] = str(root / "var/state/gallery.pid")
        cfg["guardian"]["gallery_log"] = str(root / "var/logs/gallery.log")
        return cfg

    def _ready_root(self, tmp):
        root = Path(tmp)
        (root / "var/state").mkdir(parents=True)
        (root / "var/state/status.json").write_text(json.dumps({
            "pid": os.getpid(),
            "last_frame_at": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8")
        return root

    def test_gallery_failure_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ready_root(tmp)
            g = G.Guard(self._cfg(root), dry_run=True)
            calls = []
            g._start_gallery = lambda: calls.append(True) or None
            g.check_once()
            g.check_once()
        self.assertEqual(len(calls), 1)

    def test_spawned_but_dead_gallery_counts_as_failure(self):
        """拉起成功不等于健康：相册 bind 失败后立刻退出，必须累计失败。

        真机事故：guardian 每分钟拉起一次、失败计数每次被清零，
        永远进不了退避，也永远不告警。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ready_root(tmp)
            cfg = self._cfg(root)
            cfg["guardian"]["crash_threshold"] = 2
            g = G.Guard(cfg, dry_run=True)
            g.state["last_gallery_spawn_at"] = (
                datetime.now() - timedelta(minutes=5)
            ).isoformat(timespec="seconds")
            g.state["gallery_failures"] = 1
            g._start_gallery = lambda: 999      # 进程起来了，但从不写 PID 文件
            sent = []
            g._notify = lambda text: sent.append(text)
            g.check_once()
        self.assertEqual(g.state["gallery_failures"], 2)
        self.assertTrue(any("相册" in text or "重启" in text for text in sent))

    def test_healthy_gallery_resets_failure_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ready_root(tmp)
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["gallery_failures"] = 5
            with mock.patch.object(G, "decide_gallery", return_value=G.ACT_OK):
                g._check_gallery(datetime.now())
        self.assertEqual(g.state["gallery_failures"], 0)

    def test_long_healthy_run_then_death_is_not_crash_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ready_root(tmp)
            pid_file = root / "var/state/gallery.pid"
            pid_file.write_text(str(DEAD_PID), encoding="utf-8")
            old = time.time() - 3600
            os.utime(pid_file, (old, old))
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["gallery_failures"] = 5
            g.state["last_gallery_spawn_at"] = (
                datetime.now() - timedelta(hours=2)
            ).isoformat(timespec="seconds")
            g._start_gallery = lambda: 111
            g._check_gallery(datetime.now())
        self.assertEqual(g.state["gallery_failures"], 1)

    def test_stale_pid_file_does_not_hide_crash_loop(self):
        """上一轮留下的旧 PID 文件不能被当成「刚健康运行过」。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._ready_root(tmp)
            pid_file = root / "var/state/gallery.pid"
            pid_file.write_text(str(DEAD_PID), encoding="utf-8")
            old = time.time() - 3600
            os.utime(pid_file, (old, old))
            g = G.Guard(self._cfg(root), dry_run=True)
            g.state["gallery_failures"] = 2
            g.state["last_gallery_spawn_at"] = (
                datetime.now() - timedelta(minutes=1)
            ).isoformat(timespec="seconds")
            g._start_gallery = lambda: 111
            g._notify = lambda *args, **kwargs: None
            g._check_gallery(datetime.now())
        self.assertEqual(g.state["gallery_failures"], 3)


class OwnerLockValidationTest(unittest.TestCase):
    def test_unrelated_live_pid_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "owner.lock")
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            lock = G.FileLock(path, validate=lambda pid: False)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_matching_live_pid_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "owner.lock")
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            lock = G.FileLock(path, validate=lambda pid: True)
            self.assertFalse(lock.acquire())


class StructuredEventTest(unittest.TestCase):
    def test_append_history_emits_structured_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            handler = logging.StreamHandler(buf)
            logger = logging.getLogger("guardian")
            old_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                G.append_history(Path(tmp) / "h.jsonl", {
                    "service": "watcher", "action": "restart_gone",
                    "exit_reason": "camera",
                })
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)
        out = buf.getvalue()
        self.assertIn("event=guardian_restart", out)
        self.assertIn("service=watcher", out)
        self.assertIn("exit_reason=camera", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
