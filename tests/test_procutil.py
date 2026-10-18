"""进程判活与等待的单测。

    python tests/test_procutil.py

为什么单独测这个：界面的「在线」判定、优雅停止、更新前等待，三处都依赖它。
判活写错一次，表现是「停止后点不了启动」（0.1.7 的真实事故）。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import time
import unittest

from roomwatch import procutil


class PidAliveTest(unittest.TestCase):
    def test_self_is_alive(self) -> None:
        self.assertTrue(procutil.pid_alive(os.getpid()))

    def test_zero_or_negative_is_not_alive(self) -> None:
        self.assertFalse(procutil.pid_alive(0))
        self.assertFalse(procutil.pid_alive(-1))

    def test_unused_pid_is_not_alive(self) -> None:
        # 2^31-1 不可能被分配，Windows/Linux 都返回「不存在」
        self.assertFalse(procutil.pid_alive(2**31 - 1))

    def test_returns_bool_not_none(self) -> None:
        self.assertIsInstance(procutil.pid_alive(os.getpid()), bool)


class WaitPidExitTest(unittest.TestCase):
    def test_already_dead_returns_true_immediately(self) -> None:
        started = time.monotonic()
        self.assertTrue(procutil.wait_pid_exit(2**31 - 1, timeout=5.0))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_live_process_times_out(self) -> None:
        started = time.monotonic()
        self.assertFalse(procutil.wait_pid_exit(os.getpid(), timeout=0.4))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_zero_pid_counts_as_gone(self) -> None:
        self.assertTrue(procutil.wait_pid_exit(0, timeout=1.0))


class KillTest(unittest.TestCase):
    def test_killing_dead_pid_reports_gone(self) -> None:
        self.assertTrue(procutil.kill_pid(2**31 - 1, timeout=2.0))


if __name__ == "__main__":
    unittest.main()
