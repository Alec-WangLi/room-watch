"""全局命令 `rw` 的单测：命令表、版本输出、参数翻译、自检。

    python tests/test_cli.py

这里只测「不碰硬件、不启进程」的部分：
命令表是否完整、翻译出来的 argv 对不对、doctor 的判定逻辑是否稳定。
真正拉起摄像头/相册的用例在各自的 test_*.py 里，不在这里重复。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from roomwatch import cli


class CommandTableTest(unittest.TestCase):
    def test_all_expected_commands_exist(self) -> None:
        expected = {
            "start", "dry-run", "probe", "status", "enroll", "whoami",
            "say", "say-raw", "ab", "volume-check", "volume-set", "storage",
            "gallery", "guard", "push", "push-video", "setup", "test", "doctor",
            "version", "gui",
        }
        self.assertEqual(set(cli.COMMANDS), expected)

    def test_every_command_has_description(self) -> None:
        for name, spec in cli.COMMANDS.items():
            self.assertTrue(spec.summary, msg=f"{name} 缺说明")

    def test_watcher_commands_point_at_watcher_module(self) -> None:
        for name in ("start", "probe", "status", "storage", "enroll"):
            self.assertEqual(cli.COMMANDS[name].module, "watcher", msg=name)


class TranslateTest(unittest.TestCase):
    def test_plain_start_passes_through_extra_args(self) -> None:
        self.assertEqual(cli.translate("start", ["--config", "x.yaml"]), ["--config", "x.yaml"])

    def test_status_maps_to_flag(self) -> None:
        self.assertEqual(cli.translate("status", []), ["--status"])

    def test_dry_run_maps_to_flag(self) -> None:
        self.assertEqual(cli.translate("dry-run", []), ["--dry-run"])

    def test_whoami_puts_image_positional(self) -> None:
        self.assertEqual(cli.translate("whoami", ["a.jpg", "--config", "c.yaml"]),
                         ["--whoami", "a.jpg", "--config", "c.yaml"])

    def test_enroll_from_alias(self) -> None:
        self.assertEqual(cli.translate("enroll", ["--from", "a.jpg"]), ["--enroll-from", "a.jpg"])

    def test_enroll_without_from(self) -> None:
        self.assertEqual(cli.translate("enroll", []), ["--enroll"])

    def test_volume_set_keeps_value(self) -> None:
        self.assertEqual(cli.translate("volume-set", ["70"]), ["--volume-set", "70"])

    def test_unknown_flag_is_rejected(self) -> None:
        with self.assertRaises(cli.UsageError):
            cli.translate("whoami", [])

    def test_volume_set_requires_value(self) -> None:
        with self.assertRaises(cli.UsageError):
            cli.translate("volume-set", [])


class VersionTest(unittest.TestCase):
    def test_version_prints_package_version(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["version"])
        self.assertEqual(code, 0)
        self.assertIn("room-watch", buf.getvalue())
        self.assertIn(cli.__version__, buf.getvalue())


class DoctorTest(unittest.TestCase):
    def test_doctor_returns_report_and_exit_code(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["doctor"])
        self.assertIn(code, (0, 1))
        self.assertIn("检查", buf.getvalue())

    def test_checks_cover_core_items(self) -> None:
        names = [c.name for c in cli.run_checks()]
        for expected in ("解释器", "配置文件", "依赖", "模型", "目录"):
            self.assertIn(expected, names)

    def test_missing_model_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = cli.run_checks(project_root=Path(tmp))
        model = next(c for c in checks if c.name == "模型")
        self.assertFalse(model.ok)


class TestFilesTest(unittest.TestCase):
    def test_fast_mode_skips_integration(self) -> None:
        self.assertNotIn("test_integration.py", cli.test_files(fast=True))
        self.assertIn("test_integration.py", cli.test_files(fast=False))

    def test_all_eight_suites_present(self) -> None:
        files = cli.test_files(fast=False)
        legacy = {
            "test_logic.py", "test_loudness.py", "test_recorder.py", "test_gallery.py",
            "test_guardian.py", "test_identity.py", "test_audio_storage.py",
            "test_integration.py",
        }
        self.assertTrue(legacy.issubset(set(files)), msg=str(legacy - set(files)))
        self.assertGreaterEqual(len(files), 8)


if __name__ == "__main__":
    unittest.main()
