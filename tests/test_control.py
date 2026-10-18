"""停止协议的单测：stop.request 文件 + 优雅停止/兜底强杀。

    python tests/test_control.py

0.1.7 的停止是 `taskkill /F` 一把梭：录像不收尾、音量不还原，
而界面还按心跳判活，于是「停了但启动按不动」。这里钉住两段式停止的每一环。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roomwatch import control


class StopRequestFileTest(unittest.TestCase):
    def test_missing_file_means_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(control.stop_requested(Path(tmp), 1234))
            self.assertIsNone(control.read_stop_request(Path(tmp)))

    def test_request_is_written_with_pid_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = control.request_stop(root, 4321)
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], 4321)
            self.assertTrue(data["requested_at"])
            self.assertTrue(control.stop_requested(root, 4321))

    def test_other_pid_ignores_request(self) -> None:
        """残留请求不能秒杀新起来的进程（PID 复用 / 手工清不干净）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control.request_stop(root, 4321)
            self.assertFalse(control.stop_requested(root, 9999))

    def test_clear_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control.clear_stop_request(root)  # 不存在也不许抛
            control.request_stop(root, 1)
            control.clear_stop_request(root)
            self.assertFalse(control.stop_requested(root, 1))
            control.clear_stop_request(root)

    def test_corrupt_request_is_treated_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = control.stop_request_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{oops", encoding="utf-8")
            self.assertIsNone(control.read_stop_request(root))
            self.assertFalse(control.stop_requested(root, 1))


class StopWatcherTest(unittest.TestCase):
    """alive/kill 全部注入，测试里绝不真的杀进程。"""

    def test_already_gone_returns_immediately_without_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kill = mock.Mock()
            result = control.stop_watcher(
                Path(tmp), 100, grace=5.0, alive=lambda pid: False, kill=kill,
                sleep=lambda s: None,
            )
        self.assertEqual(result["method"], "already-stopped")
        kill.assert_not_called()

    def test_graceful_when_process_exits_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = {"n": 0}
            kill = mock.Mock()

            def alive(pid: int) -> bool:
                calls["n"] += 1
                return calls["n"] < 3      # 第 3 次检查时已经退出

            result = control.stop_watcher(root, 100, grace=5.0, alive=alive, kill=kill,
                                          sleep=lambda s: None)
            self.assertEqual(result["method"], "graceful")
            kill.assert_not_called()
            self.assertFalse(control.stop_requested(root, 100))

    def test_force_kill_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"alive": True}

            def kill(pid: int, timeout: float = 5.0) -> bool:
                state["alive"] = False
                return True

            result = control.stop_watcher(
                root, 100, grace=0.0, alive=lambda pid: state["alive"], kill=kill,
                sleep=lambda s: None,
            )
            self.assertEqual(result["method"], "forced")
            self.assertFalse(control.stop_requested(root, 100))

    def test_force_disabled_reports_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = control.stop_watcher(
                root, 100, grace=0.0, force=False, alive=lambda pid: True,
                kill=mock.Mock(), sleep=lambda s: None,
            )
            self.assertEqual(result["method"], "timeout")
            self.assertTrue(control.stop_requested(root, 100))

    def test_request_file_written_before_waiting(self) -> None:
        """必须先落请求再等：反过来的话进程永远收不到通知，只能等强杀。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}

            def alive(pid: int) -> bool:
                seen["exists"] = control.stop_requested(root, pid)
                return False

            control.stop_watcher(root, 100, grace=1.0, alive=alive, kill=mock.Mock(),
                                 sleep=lambda s: None)
        self.assertTrue(seen.get("exists"))


if __name__ == "__main__":
    unittest.main()
