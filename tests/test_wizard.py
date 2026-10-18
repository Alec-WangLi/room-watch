"""首次配置向导的单测。

    python tests/test_wizard.py

只测纯逻辑（解析 / 合并 / 生成的启动脚本），不真的开摄像头、不写注册表、
不发网络请求 —— 这些副作用在客户机上无法重复，也不该由单测承担。

重点覆盖三类会静默出错的输入：
1. `0`（字符串）与 `0`（整数）的区别 —— 写错就变成「摄像头编号 = "0"」；
2. webhook 粘完整地址 / 只粘 key —— 历史坑是双嵌套 `?key=https://...?key=xxx`；
3. 合并而不是覆盖 —— 先配推送再配摄像头，两次都不能冲掉对方。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from roomwatch import wizard


class ParseCameraSourceTest(unittest.TestCase):
    def test_digit_becomes_index(self) -> None:
        self.assertEqual(wizard.parse_camera_source("1"), {"index": 1, "source": None})

    def test_zero_is_integer_not_string(self) -> None:
        """YAML 里 "0" 与 0 是两种类型，写成字符串会让 OpenCV 打不开设备。"""
        got = wizard.parse_camera_source("0")
        self.assertIsInstance(got["index"], int)
        self.assertIsNone(got["source"])

    def test_empty_falls_back_to_default_device(self) -> None:
        self.assertEqual(wizard.parse_camera_source("   "), {"index": 0, "source": None})

    def test_rtsp_url_is_kept_verbatim(self) -> None:
        url = "rtsp://admin:pass123@192.168.1.64:554/Streaming/Channels/101"
        got = wizard.parse_camera_source(url)
        self.assertEqual(got["source"], url)
        self.assertEqual(got["backend"], "ffmpeg")

    def test_quoted_url_is_stripped(self) -> None:
        got = wizard.parse_camera_source('"rtsp://1.2.3.4/live"')
        self.assertEqual(got["source"], "rtsp://1.2.3.4/live")

    def test_existing_file_becomes_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"")
            got = wizard.parse_camera_source(str(video))
            self.assertTrue(Path(got["source"]).is_absolute())
            self.assertTrue(Path(got["source"]).is_file())

    def test_unknown_input_raises_with_hint(self) -> None:
        """不能静默当成 0：用户会以为配好了，其实在拍另一台设备。"""
        with self.assertRaises(ValueError) as ctx:
            wizard.parse_camera_source("客厅的摄像头")
        self.assertIn("rtsp://", str(ctx.exception))

    def test_truncated_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            wizard.parse_camera_source("rtsp://")


class BuildLocalTest(unittest.TestCase):
    def test_full_webhook_is_normalized(self) -> None:
        got = wizard.build_local({}, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123")
        self.assertEqual(
            got["notify"]["wecom_bot"]["webhook"],
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123",
        )
        self.assertEqual(got["notify"]["channel"], "wecom_bot")

    def test_bare_key_becomes_webhook(self) -> None:
        got = wizard.build_local({}, "00000000-0000-0000-0000-000000000000")
        self.assertTrue(got["notify"]["wecom_bot"]["webhook"].endswith("key=00000000-0000-0000-0000-000000000000"))

    def test_empty_webhook_omits_notify_block(self) -> None:
        """跳过推送时不能写一个空 notify，否则会把主配置的渠道覆盖成空串。"""
        self.assertNotIn("notify", wizard.build_local({}))

    def test_token_only_keeps_loopback(self) -> None:
        got = wizard.build_local({}, "", "0123456789ab")
        self.assertEqual(got["gallery"], {"token": "0123456789ab"})

    def test_token_with_lan_opens_host(self) -> None:
        got = wizard.build_local({}, "", "0123456789ab", allow_lan=True)
        self.assertEqual(got["gallery"]["host"], "0.0.0.0")

    def test_lan_without_token_is_ignored(self) -> None:
        """没有 token 时绝不能绑 0.0.0.0 —— gallery 自己会拒绝启动，但这里先不写。"""
        self.assertNotIn("gallery", wizard.build_local({}, "", "", allow_lan=True))


class MergeLocalTest(unittest.TestCase):
    def test_creates_file_with_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            wizard.merge_local(path, {"camera": {"index": 0}})
            text = path.read_text(encoding="utf-8")
            self.assertIn("不入库", text)
            self.assertEqual(yaml.safe_load(text)["camera"]["index"], 0)

    def test_second_run_keeps_earlier_keys(self) -> None:
        """先配推送、再配摄像头：推送凭据不能被冲掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            wizard.merge_local(path, {"notify": {"channel": "wecom_bot",
                                                 "wecom_bot": {"webhook": "u1"}}})
            wizard.merge_local(path, {"camera": {"index": 1}})
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["camera"]["index"], 1)
            self.assertEqual(data["notify"]["wecom_bot"]["webhook"], "u1")

    def test_same_key_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            wizard.merge_local(path, {"camera": {"index": 0}})
            wizard.merge_local(path, {"camera": {"index": 2}})
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["camera"]["index"], 2)

    def test_broken_existing_file_is_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.local.yaml"
            path.write_text("not: [valid", encoding="utf-8")
            wizard.merge_local(path, {"camera": {"index": 0}})
            self.assertIn("camera", yaml.safe_load(path.read_text(encoding="utf-8")))


class AutostartTest(unittest.TestCase):
    """自启实现已搬到 autostart.py（界面设置共用）。这里只钉住「向导确实转发了」。"""

    def test_install_forwards_to_autostart_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(wizard, "enable_autostart",
                                  return_value=(True, "ok")) as fake:
            ok, msg = wizard.install_autostart(Path(tmp), "C:/x/python.exe",
                                               Path(tmp) / "config.yaml")
        self.assertTrue(ok)
        self.assertEqual(msg, "ok")
        fake.assert_called_once()

    def test_remove_forwards_to_autostart_module(self) -> None:
        with mock.patch.object(wizard, "disable_autostart",
                               return_value=(False, "本来就没有注册过")) as fake:
            ok, msg = wizard.remove_autostart()
        self.assertFalse(ok)
        self.assertIn("没有", msg)
        fake.assert_called_once()


class CheckModeTest(unittest.TestCase):
    def test_summary_hides_rtsp_password(self) -> None:
        """--check 会被人截图发群里，密码不能原文打印。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(
                "camera:\n  index: 0\n  source: rtsp://admin:secret123@10.0.0.9:554/s\n"
                "notify:\n  channel: wecom_bot\n"
                "gallery:\n  token: abcdefghijklmn\n",
                encoding="utf-8",
            )
            lines = "\n".join(wizard.summarize(root / "config.yaml"))
        self.assertNotIn("secret123", lines)
        self.assertIn("10.0.0.9", lines)

    def test_check_mode_runs_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = wizard.main(["--root", tmp, "--check"])
        self.assertEqual(code, 2)

    def test_short_token_is_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text("camera:\n  index: 0\n", encoding="utf-8")
            code = wizard.main(["--root", tmp, "--camera", "0", "--gallery-token", "short",
                                "--no-probe", "--no-test", "--no-autostart"])
            self.assertEqual(code, 2)
            self.assertFalse((root / "config.local.yaml").exists())


class DoctorIntegrationTest(unittest.TestCase):
    def test_doctor_reports_runtime_and_ffmpeg(self) -> None:
        from roomwatch import cli

        names = [c.name for c in cli.run_checks()]
        self.assertIn("内嵌运行时", names)
        self.assertIn("ffmpeg", names)


if __name__ == "__main__":
    unittest.main()
