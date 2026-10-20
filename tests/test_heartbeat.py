"""外部心跳与 guardian 关键通知的单测。

    python tests/test_heartbeat.py

不碰真实网络：HTTP 用注入的假函数；时间用传入的 now。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from roomwatch import guardian as G
from roomwatch import heartbeat as H


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status


class HeartbeatConfigTest(unittest.TestCase):
    def test_placeholders_are_treated_as_unset(self):
        cfg = {"monitor": {"heartbeat_url": "https://hc-ping.com/REPLACE_ME",
                           "heartbeat_interval_seconds": "60"}}
        hb = H.load_heartbeat(cfg)
        self.assertFalse(hb.configured)
        self.assertEqual(hb.interval_seconds, 60)

    def test_values_are_parsed(self):
        cfg = {"monitor": {"heartbeat_url": "https://hc.test/abc",
                           "heartbeat_fail_url": "https://hc.test/abc/fail",
                           "heartbeat_interval_seconds": 120}}
        hb = H.load_heartbeat(cfg)
        self.assertTrue(hb.configured)
        self.assertTrue(hb.has_fail_url)
        self.assertEqual(hb.interval_seconds, 120)

    def test_due(self):
        now = datetime(2026, 10, 19, 12, 0, 0)
        self.assertTrue(H.heartbeat_due(None, now, 300))
        self.assertFalse(H.heartbeat_due(
            (now - timedelta(seconds=100)).isoformat(timespec="seconds"), now, 300))
        self.assertTrue(H.heartbeat_due(
            (now - timedelta(seconds=301)).isoformat(timespec="seconds"), now, 300))

    def test_ping_success_and_failure(self):
        self.assertEqual(H.ping("https://x", http=lambda *a, **k: FakeResponse(200)),
                         (True, ""))
        ok, error = H.ping("https://x", http=lambda *a, **k: FakeResponse(503))
        self.assertFalse(ok)
        self.assertIn("503", error)

    def test_ping_exception_is_swallowed(self):
        def boom(*args, **kwargs):
            raise OSError("断网")
        ok, error = H.ping("https://x", http=boom)
        self.assertFalse(ok)
        self.assertIn("断网", error)


class HeartbeatSenderTest(unittest.TestCase):
    def _sender(self, cfg, state=None):
        calls = []

        def http(url, timeout=10):
            calls.append(url)
            return FakeResponse(200)

        state = state if state is not None else {}
        sender = H.HeartbeatSender(
            H.load_heartbeat(cfg), state, lambda: None,
            http=http, clock=lambda: datetime(2026, 10, 19, 12, 0, 0),
        )
        return sender, calls, state

    def test_no_url_is_disabled(self):
        sender, calls, _ = self._sender({})
        self.assertEqual(sender.maybe_send(healthy=True, paused=False), "disabled")
        self.assertEqual(calls, [])

    def test_healthy_pings_and_records(self):
        sender, calls, state = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok",
                         "heartbeat_interval_seconds": 300}})
        self.assertEqual(sender.maybe_send(healthy=True, paused=False), "ok")
        self.assertEqual(calls, ["https://hc.test/ok"])
        self.assertTrue(state["last_heartbeat_ok"])
        self.assertEqual(state["heartbeat_fail_streak"], 0)

    def test_paused_stops(self):
        sender, calls, _ = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok"}})
        self.assertEqual(sender.maybe_send(healthy=True, paused=True), "paused")
        self.assertEqual(calls, [])

    def test_interval_skips_second_ping(self):
        sender, calls, _ = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok",
                         "heartbeat_interval_seconds": 300}})
        sender.maybe_send(healthy=True, paused=False)
        sender.maybe_send(healthy=True, paused=False)
        self.assertEqual(calls, ["https://hc.test/ok"])

    def test_unhealthy_uses_fail_url(self):
        sender, calls, state = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok",
                         "heartbeat_fail_url": "https://hc.test/fail",
                         "heartbeat_interval_seconds": 300}})
        self.assertEqual(sender.maybe_send(healthy=False, paused=False), "fail")
        self.assertEqual(calls, ["https://hc.test/fail"])
        self.assertEqual(state["heartbeat_fail_streak"], 1)

    def test_unhealthy_without_fail_url_stops_pinging(self):
        sender, calls, state = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok"}})
        self.assertEqual(sender.maybe_send(healthy=False, paused=False), "stopped")
        self.assertEqual(calls, [])

    def test_recovery_pings_even_if_interval_not_due(self):
        state = {"last_heartbeat_at": "2026-10-19T11:59:50",
                 "heartbeat_fail_streak": 2}
        sender, calls, _ = self._sender(
            {"monitor": {"heartbeat_url": "https://hc.test/ok",
                         "heartbeat_interval_seconds": 300}}, state)
        self.assertEqual(sender.maybe_send(healthy=True, paused=False), "ok")
        self.assertEqual(calls, ["https://hc.test/ok"])

    def test_ping_failure_records_error_without_raising(self):
        def boom(url, timeout=10):
            raise OSError("断开")
        sender = H.HeartbeatSender(
            H.load_heartbeat({"monitor": {"heartbeat_url": "https://hc.test/ok"}}),
            {}, lambda: None, http=boom,
            clock=lambda: datetime(2026, 10, 19, 12, 0, 0),
        )
        self.assertEqual(sender.maybe_send(healthy=True, paused=False), "fail")
        self.assertFalse(sender.state["last_heartbeat_ok"])
        self.assertIn("断开", sender.state["last_heartbeat_error"])

    def test_start_url(self):
        sender, calls, state = self._sender(
            {"monitor": {"heartbeat_start_url": "https://hc.test/start"}})
        sender.note_start()
        self.assertEqual(calls, ["https://hc.test/start"])
        self.assertIn("last_heartbeat_start_at", state)


class GuardianOutboxTest(unittest.TestCase):
    def _cfg(self, root: Path) -> dict:
        return {
            "guardian": {
                "desired_state": str(root / "var/state/desired_state.json"),
                "state": str(root / "var/state/guardian_state.json"),
                "lock": str(root / "var/state/guardian.lock"),
                "history": str(root / "var/state/h.jsonl"),
                "gallery_pid": str(root / "var/state/gallery.pid"),
                "gallery_log": str(root / "var/logs/gallery.log"),
                "supervise_gallery": False,
            },
            "health": {"path": str(root / "var/state/health.json"),
                       "exit_record": str(root / "var/state/last_exit.json")},
            "storage": {"status": str(root / "var/state/status.json"),
                        "database": str(root / "var/db/events.db")},
            "notify": {"channels": ["wecom_bot"],
                       "wecom_bot": {"webhook": "https://x.test/send?key=K"}},
            "__config_path__": str(root / "config.yaml"),
        }

    def test_critical_notification_is_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            g = G.Guard(self._cfg(root), dry_run=False)
            try:
                g._notify("反复崩溃")
                self.assertGreaterEqual(g.outbox.pending_count(), 1)
            finally:
                if g.outbox is not None:
                    g.outbox.close()

    def test_heartbeat_alert_after_three_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            g = G.Guard(self._cfg(root), dry_run=False)
            g.heartbeat = SimpleNamespace(
                maybe_send=lambda **kwargs: "stopped",
                failure_streak=3,
            )
            sent = []
            g._notify = lambda text, severity="critical": sent.append((text, severity))
            g._heartbeat(datetime.now(), G.ACT_RESTART_GONE)
        self.assertTrue(sent)
        self.assertEqual(sent[0][1], "critical")

    def test_heartbeat_skips_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/state").mkdir(parents=True)
            g = G.Guard(self._cfg(root), dry_run=False)
            calls = []
            g.heartbeat = SimpleNamespace(
                maybe_send=lambda **kwargs: calls.append(kwargs) or "ok",
                failure_streak=0,
            )
            self.assertEqual(g._heartbeat(datetime.now(), G.ACT_STARTED), "starting")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
