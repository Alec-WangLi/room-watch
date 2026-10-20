"""健康快照、退出记录与停滞判定的单测。

    python tests/test_health.py

这些逻辑必须完全离线、不碰摄像头；watcher 接不接入由集成测试覆盖。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from roomwatch import health


class AtomicJsonTest(unittest.TestCase):
    def test_write_leaves_no_tmp_and_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state" / "health.json"
            health.write_json_atomic(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})
            self.assertFalse((p.parent / "health.json.tmp").exists())

    def test_read_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{oops", encoding="utf-8")
            self.assertIsNone(health.read_json(p))

    def test_read_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(health.read_json(Path(tmp) / "nope.json"))

    def test_concurrent_writers_do_not_collide(self):
        """GUI 与 CLI 可能同时写 desired_state；临时文件名不能共用。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            errors = []
            barrier = threading.Barrier(8)

            def writer(index):
                try:
                    barrier.wait()
                    for n in range(50):
                        health.write_json_atomic(p, {"writer": index, "n": n})
                except Exception as exc:  # noqa: BLE001 - 失败要带回主线程
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertIn("writer", json.loads(p.read_text(encoding="utf-8")))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class HealthPayloadTest(unittest.TestCase):
    def test_fixed_keys_and_extra(self):
        payload = health.build_health_payload(
            pid=123,
            loop_seq=7,
            started_at="2026-10-19T12:00:00",
            last_frame_at="2026-10-19T12:00:01",
            last_inference_at="2026-10-19T12:00:02",
            consecutive_errors=2,
            camera_reopens=3,
            selfcheck=[{"name": "模型", "ok": True, "detail": "ok"}],
            extra={"recording": True},
        )
        self.assertEqual(payload["pid"], 123)
        self.assertEqual(payload["loop_seq"], 7)
        self.assertEqual(payload["consecutive_errors"], 2)
        self.assertEqual(payload["camera_reopens"], 3)
        self.assertEqual(payload["selfcheck"][0]["name"], "模型")
        self.assertTrue(payload["recording"])
        self.assertIn("rss_mb", payload)
        self.assertIn("cpu_pct", payload)
        self.assertIn("updated_at", payload)


class ExitRecordTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            health.write_exit_record(
                p, pid=123, code=health.EXIT_UNHANDLED, reason="unhandled"
            )
            rec = health.read_exit_record(p)
            self.assertEqual((rec["pid"], rec["code"], rec["reason"]), (123, 5, "unhandled"))
            self.assertTrue(rec["at"])

    def test_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "last_exit.json"
            p.write_text("{oops", encoding="utf-8")
            self.assertIsNone(health.read_exit_record(p))

    def test_exit_codes_are_distinct(self):
        codes = [
            health.EXIT_OK,
            health.EXIT_CONFIG,
            health.EXIT_CAMERA,
            health.EXIT_MODEL,
            health.EXIT_UNHANDLED,
            health.EXIT_STALLED,
        ]
        self.assertEqual(len(codes), len(set(codes)))


class StallTest(unittest.TestCase):
    def test_strictly_greater_than_threshold(self):
        self.assertFalse(health.stall_exceeded(1, 1, 120.0, 120.0))
        self.assertTrue(health.stall_exceeded(1, 1, 120.1, 120.0))

    def test_progress_resets(self):
        self.assertFalse(health.stall_exceeded(2, 1, 9999.0, 120.0))


class WatchdogLoopTest(unittest.TestCase):
    def test_stall_dumps_then_stops(self):
        seq = {"v": 1}
        clock = {"t": 0.0}
        calls = []
        stop = threading.Event()

        def sleep(seconds):
            clock["t"] += seconds

        def on_stall():
            calls.append("stall")
            stop.set()

        health.watchdog_loop(
            get_seq=lambda: seq["v"],
            dump_stacks=lambda: calls.append("dump"),
            on_stall=on_stall,
            sleep=sleep,
            stop_event=stop,
            clock=lambda: clock["t"],
            poll_seconds=5.0,
            threshold_seconds=10.0,
        )
        self.assertEqual(calls, ["dump", "stall"])

    def test_progress_prevents_stall(self):
        seq = {"v": 1}
        clock = {"t": 0.0}
        calls = []
        stop = threading.Event()
        sleeps = {"n": 0}

        def sleep(seconds):
            sleeps["n"] += 1
            clock["t"] += seconds
            seq["v"] += 1          # 每轮都有进展
            if sleeps["n"] >= 5:
                stop.set()

        health.watchdog_loop(
            get_seq=lambda: seq["v"],
            dump_stacks=lambda: calls.append("dump"),
            on_stall=lambda: calls.append("stall"),
            sleep=sleep,
            stop_event=stop,
            clock=lambda: clock["t"],
            poll_seconds=5.0,
            threshold_seconds=10.0,
        )
        self.assertEqual(calls, [])


class WatcherHealthTest(unittest.TestCase):
    def test_write_health_contains_runtime_fields(self):
        from roomwatch.watcher import Watcher

        with tempfile.TemporaryDirectory() as tmp:
            w = object.__new__(Watcher)
            w.health_path = str(Path(tmp) / "health.json")
            w.loop_seq = 7
            w.started_at = datetime.now()
            w.last_frame_at = datetime.now()
            w.last_inference_at = datetime.now()
            w.consecutive_errors = 2
            w.camera_reopens = 3
            w.selfcheck = [{"name": "模型", "ok": True, "detail": "ok"}]
            w.exit_reason = "normal"
            w.write_health()
            data = json.loads(Path(w.health_path).read_text(encoding="utf-8"))
            self.assertEqual(data["loop_seq"], 7)
            self.assertEqual(data["consecutive_errors"], 2)
            self.assertEqual(data["camera_reopens"], 3)
            self.assertEqual(data["selfcheck"][0]["name"], "模型")
            self.assertIn("rss_mb", data)


class ProcessStatsTest(unittest.TestCase):
    def test_process_handle_is_reused(self):
        self.assertIs(health._process(), health._process())

    def test_proc_stats_returns_numbers(self):
        rss, cpu = health.proc_stats()
        self.assertIsNotNone(rss)
        self.assertIsNotNone(cpu)


class WatcherExitRecordTest(unittest.TestCase):
    def test_record_exit_prefers_diagnostics(self):
        from roomwatch.watcher import Watcher

        class FakeDiagnostics:
            def __init__(self):
                self.calls = []

            def note_exit(self, code, reason):
                self.calls.append((code, reason))

        w = object.__new__(Watcher)
        w.diagnostics = FakeDiagnostics()
        w.exit_record_path = "unused.json"
        w._record_exit(health.EXIT_STALLED, "stalled")
        self.assertEqual(w.diagnostics.calls, [(health.EXIT_STALLED, "stalled")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
