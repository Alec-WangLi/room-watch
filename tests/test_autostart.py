"""开机自启的单测：不碰真注册表，读写全部注入。

    python tests/test_autostart.py

为什么不再用 `reg add`：本机策略与沙箱都会拦外部 reg.exe，
而且返回值要靠解析 stdout，出错信息很难读。winreg 是进程内的，失败了有异常类型。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roomwatch import autostart, procutil

ROOT = Path("C:/Users/Alec/AppData/Local/Programs/room-watch")
PYTHONW = "C:/Users/Alec/AppData/Local/Programs/room-watch/runtime/python/pythonw.exe"


class ScriptTest(unittest.TestCase):
    def test_script_sets_cwd_and_runs_guardian(self) -> None:
        text = autostart.autostart_script(ROOT, PYTHONW, ROOT / "config.yaml")
        self.assertIn(f'sh.CurrentDirectory = "{ROOT}"', text)
        self.assertIn("pythonw.exe", text)
        self.assertIn("-m roomwatch guard", text)
        self.assertIn("guardian.log", text)

    def test_command_line_quotes_script(self) -> None:
        cmd = autostart.command_line(ROOT)
        self.assertTrue(cmd.startswith("wscript.exe "), cmd)
        self.assertIn("start-monitor.vbs", cmd)
        self.assertIn('"', cmd)


class StatusTest(unittest.TestCase):
    def test_missing_value_is_disabled(self) -> None:
        info = autostart.status(ROOT, read=lambda: None,
                                runner=lambda cmd: (1, "", "blocked"), platform="win32")
        self.assertFalse(info["enabled"])
        self.assertFalse(info["points_to_this_install"])

    def test_value_pointing_here_is_enabled(self) -> None:
        target = autostart.command_line(ROOT)
        info = autostart.status(ROOT, read=lambda: target,
                                runner=lambda cmd: (1, "", "blocked"), platform="win32")
        self.assertTrue(info["enabled"])
        self.assertTrue(info["points_to_this_install"])
        self.assertEqual(info["command"], target)

    def test_value_pointing_elsewhere_is_enabled_but_flagged(self) -> None:
        info = autostart.status(ROOT, read=lambda: 'wscript.exe "D:\\old\\start-monitor.vbs"',
                                runner=lambda cmd: (1, "", "blocked"), platform="win32")
        self.assertTrue(info["enabled"])
        self.assertFalse(info["points_to_this_install"])

    def test_script_existence_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = autostart.status(root, read=lambda: None,
                                    runner=lambda cmd: (1, "", "blocked"), platform="win32")
            self.assertFalse(info["script_exists"])


class EnableDisableTest(unittest.TestCase):
    def test_enable_writes_script_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            pythonw = root / "runtime" / "python" / "pythonw.exe"
            pythonw.write_bytes(b"x")
            written = {}

            ok, msg = autostart.enable(
                root, str(pythonw), root / "config.yaml",
                write=lambda value: written.setdefault("value", value) or True,
                runner=lambda cmd: (1, "", "blocked"),
                platform="win32",
            )
            self.assertTrue(ok, msg)
            self.assertTrue((root / "start-monitor.vbs").is_file())
            self.assertEqual(written["value"], autostart.command_line(root))

    def test_enable_without_pythonw_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, msg = autostart.enable(root, str(root / "nope" / "python.exe"),
                                       root / "config.yaml",
                                       write=lambda value: True, platform="win32")
        self.assertFalse(ok)
        self.assertIn("pythonw.exe", msg)

    def test_enable_on_non_windows_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, msg = autostart.enable(Path(tmp), PYTHONW, Path(tmp) / "config.yaml",
                                       write=lambda value: True, platform="linux")
        self.assertFalse(ok)
        self.assertIn("Windows", msg)

    def test_registry_error_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            pythonw = root / "runtime" / "python" / "pythonw.exe"
            pythonw.write_bytes(b"x")

            def boom(value: str) -> bool:
                raise OSError("拒绝访问")

            ok, msg = autostart.enable(root, str(pythonw), root / "config.yaml",
                                       write=boom,
                                       runner=lambda cmd: (1, "", "blocked"),
                                       platform="win32")
        self.assertFalse(ok)
        self.assertIn("拒绝访问", msg)

    def test_disable_reports_when_not_registered(self) -> None:
        ok, msg = autostart.disable(delete=lambda: False,
                                    runner=lambda cmd: (1, "", "blocked"), platform="win32")
        self.assertFalse(ok)
        self.assertIn("没有", msg)

    def test_disable_success(self) -> None:
        ok, msg = autostart.disable(delete=lambda: True,
                                    runner=lambda cmd: (1, "", "blocked"), platform="win32")
        self.assertTrue(ok, msg)


def runner_ok(cmd):
    return (0, "", "")


class TaskSchedulerTest(unittest.TestCase):
    def test_task_xml_has_logon_trigger_and_restart(self):
        xml = autostart.task_xml(ROOT, PYTHONW, ROOT / "config.yaml",
                                 ROOT / "var/logs/guardian.log")
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn("-m roomwatch guard", xml)
        self.assertIn(str(ROOT), xml)
        self.assertIn("IgnoreNew", xml)

    def test_enable_prefers_task_then_removes_legacy_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            deleted = []
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml", write=lambda v: True,
                                       delete=lambda: deleted.append(True) or True,
                                       runner=runner_ok, platform="win32")
            self.assertTrue(ok, msg)
            self.assertIn("计划任务", msg)
            self.assertTrue(deleted)

    def test_enable_falls_back_to_run_when_task_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            written = {}
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml",
                                       write=lambda v: written.setdefault("v", v) or True,
                                       delete=lambda: True,
                                       runner=lambda cmd: (1, "", "blocked"),
                                       platform="win32")
            self.assertTrue(ok, msg)
            self.assertIn("Run", msg)
            self.assertIn("v", written)

    def test_enable_reports_failure_when_both_paths_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime" / "python").mkdir(parents=True)
            (root / "runtime" / "python" / "pythonw.exe").write_bytes(b"x")
            ok, msg = autostart.enable(root, str(root / "runtime/python/pythonw.exe"),
                                       root / "config.yaml", write=lambda v: False,
                                       delete=lambda: True,
                                       runner=lambda cmd: (1, "", "blocked"),
                                       platform="win32")
            self.assertFalse(ok)
            self.assertIn("失败", msg)


class BackendStatusTest(unittest.TestCase):
    def test_task_backend(self):
        info = autostart.status(ROOT, read=lambda: None,
                                runner=lambda cmd: (0, "", ""), platform="win32")
        self.assertEqual(info["backend"], "task")
        self.assertFalse(info["conflict"])

    def test_conflict_when_both_exist(self):
        info = autostart.status(ROOT, read=lambda: autostart.command_line(ROOT),
                                runner=lambda cmd: (0, "", ""), platform="win32")
        self.assertEqual(info["backend"], "conflict")
        self.assertTrue(info["conflict"])


class DefaultRunnerTest(unittest.TestCase):
    def test_schtasks_is_hidden_when_parent_has_no_console(self):
        """界面里切换自启时，schtasks 不能弹控制台。"""
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(autostart.subprocess, "run", return_value=fake) as run:
            autostart._default_runner(["schtasks", "/Query"])
        self.assertEqual(
            run.call_args.kwargs.get("creationflags"), procutil.no_window_flags()
        )


if __name__ == "__main__":
    unittest.main()
