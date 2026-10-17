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

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
