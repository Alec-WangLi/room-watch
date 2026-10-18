"""音量策略的单测：三档模式 + 崩溃后还原。

    python tests/test_volume_mode.py

0.1.7 的真实事故：日志 `03:11:07 主音量 2.0% → 70.0%`，随后进程被强杀，
还原代码没机会跑，系统音量就永久停在 70%。而录音走的是麦克风输入，
跟系统播放音量毫无关系——抬它只是为了 TTS 播报能被听见。
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

from roomwatch import audio_device


class ResolveModeTest(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertEqual(audio_device.resolve_mode({}), "off")
        self.assertEqual(audio_device.resolve_mode({"ensure_system_volume": 0}), "off")

    def test_explicit_mode_wins(self) -> None:
        for mode in ("off", "session", "master"):
            self.assertEqual(
                audio_device.resolve_mode({"volume_mode": mode,
                                           "ensure_system_volume": 70}), mode)

    def test_legacy_config_keeps_old_behaviour(self) -> None:
        """老配置只有 ensure_system_volume，按 master 解释，行为不变。"""
        self.assertEqual(audio_device.resolve_mode({"ensure_system_volume": 70}), "master")

    def test_unknown_mode_falls_back_to_legacy_rule(self) -> None:
        self.assertEqual(audio_device.resolve_mode({"volume_mode": "loud"}), "off")
        self.assertEqual(
            audio_device.resolve_mode({"volume_mode": "loud", "ensure_system_volume": 55}),
            "master")

    def test_yaml_boolean_is_understood(self) -> None:
        """YAML 1.1 会把裸 off/on 读成 False/True，不能因此变成「未知模式」。"""
        self.assertEqual(audio_device.resolve_mode({"volume_mode": False}), "off")
        self.assertEqual(audio_device.resolve_mode({"volume_mode": True}), "master")

    def test_case_and_space_are_tolerated(self) -> None:
        self.assertEqual(audio_device.resolve_mode({"volume_mode": " Session "}), "session")


class GuardOffTest(unittest.TestCase):
    def test_off_never_touches_anything(self) -> None:
        guard = audio_device.VolumeGuard(mode="off", ensure_percent=70)
        with mock.patch.object(audio_device, "available", return_value=True), \
             mock.patch.object(audio_device, "set_master_percent") as setter, \
             mock.patch.object(audio_device, "get_master_percent",
                               return_value=2.0), \
             mock.patch.object(audio_device, "is_muted", return_value=False):
            message = guard.apply()
        self.assertIn("不碰系统音量", message)
        self.assertFalse(guard.changed)
        setter.assert_not_called()
        self.assertEqual(guard.restore(), "")


class GuardSessionTest(unittest.TestCase):
    def test_session_only_touches_own_track(self) -> None:
        guard = audio_device.VolumeGuard(mode="session", ensure_percent=70)
        with mock.patch.object(audio_device, "available", return_value=True), \
             mock.patch.object(audio_device, "set_master_percent") as setter, \
             mock.patch.object(audio_device, "set_app_session_percent",
                               return_value=1) as session:
            message = guard.apply()
        self.assertIn("音轨", message)
        self.assertIn("不动系统音量", message)
        setter.assert_not_called()
        session.assert_called_once()
        self.assertEqual(guard.restore(), "")

    def test_session_without_audio_session_is_explained(self) -> None:
        guard = audio_device.VolumeGuard(mode="session", ensure_percent=70)
        with mock.patch.object(audio_device, "available", return_value=True), \
             mock.patch.object(audio_device, "set_app_session_percent", return_value=0):
            message = guard.apply()
        self.assertIn("播报时", message)


class GuardMasterTest(unittest.TestCase):
    def test_master_raises_volume_and_writes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "volume_backup.json"
            guard = audio_device.VolumeGuard(mode="master", ensure_percent=70,
                                             backup_path=backup)
            with mock.patch.object(audio_device, "available", return_value=True), \
                 mock.patch.object(audio_device, "get_master_percent", return_value=2.0), \
                 mock.patch.object(audio_device, "is_muted", return_value=False), \
                 mock.patch.object(audio_device, "set_master_percent",
                                   return_value=True) as setter:
                message = guard.apply()
            self.assertTrue(guard.changed)
            self.assertIn("2.0% → 70", message)
            setter.assert_called_once()
            data = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(data["percent"], 2.0)
            self.assertEqual(data["pid"], _os.getpid())

    def test_master_leaves_high_volume_alone(self) -> None:
        guard = audio_device.VolumeGuard(mode="master", ensure_percent=70)
        with mock.patch.object(audio_device, "available", return_value=True), \
             mock.patch.object(audio_device, "get_master_percent", return_value=85.0), \
             mock.patch.object(audio_device, "is_muted", return_value=False), \
             mock.patch.object(audio_device, "set_master_percent") as setter:
            message = guard.apply()
        self.assertIn("已达标", message)
        self.assertFalse(guard.changed)
        setter.assert_not_called()


class RestoreBackupTest(unittest.TestCase):
    def _write(self, path: Path, pid: int) -> None:
        path.write_text(json.dumps({"percent": 2.0, "muted": False, "pid": pid,
                                    "at": "2026-10-19T03:11:07"}), encoding="utf-8")

    def test_missing_backup_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            message = audio_device.restore_backup(Path(tmp) / "none.json", alive=lambda p: False)
        self.assertEqual(message, "")

    def test_dead_pid_triggers_restore_and_cleans_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volume_backup.json"
            self._write(path, 4321)
            with mock.patch.object(audio_device, "available", return_value=True), \
                 mock.patch.object(audio_device, "set_master_percent",
                                   return_value=True) as setter:
                message = audio_device.restore_backup(path, alive=lambda pid: False)
            self.assertIn("2.0%", message)
            setter.assert_called_once()
            self.assertFalse(path.exists())

    def test_live_pid_leaves_backup_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volume_backup.json"
            self._write(path, _os.getpid())
            with mock.patch.object(audio_device, "set_master_percent") as setter:
                message = audio_device.restore_backup(path, alive=lambda pid: True)
            self.assertEqual(message, "")
            setter.assert_not_called()
            self.assertTrue(path.exists())

    def test_corrupt_backup_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volume_backup.json"
            path.write_text("{oops", encoding="utf-8")
            message = audio_device.restore_backup(path, alive=lambda pid: False)
            self.assertEqual(message, "")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
