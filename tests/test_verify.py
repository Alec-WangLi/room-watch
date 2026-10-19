"""「真的在工作」验证的单元测试：不碰摄像头、不联网。

    python tests/test_verify.py

所有外部动作（自检、摄像头、推送、心跳）都通过参数注入，
这样测试只验证聚合逻辑与判定规则。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from roomwatch import verify


def make_project(root: Path, extra: str = "") -> Path:
    (root / "config.yaml").write_text(
        "notify:\n  channel: none\n"
        "monitor:\n  heartbeat_url: ''\n" + extra,
        encoding="utf-8",
    )
    return root


class FakeNotifier:
    def __init__(self, ok: bool = True, error: str = ""):
        self.ok = ok
        self.error = error
        self.calls = []

    def deliver(self, channel, kind, payload, **_kwargs):
        self.calls.append((channel, kind))
        return SimpleNamespace(ok=self.ok, error=self.error)


class VerifyTest(unittest.TestCase):
    def test_all_good_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp),
                                "notify:\n  channels: [wecom_bot]\n")
            result = verify.run_checks(
                root,
                selfcheck_items=lambda: [{"name": "自检 · 模型", "ok": True,
                                          "detail": "ok", "required": True}],
                probe=lambda _p: (True, "1280x720"),
                notifier=FakeNotifier(),
                ping=lambda *_a, **_k: (True, ""),
            )
        self.assertTrue(result["ok"], result)
        names = [i["name"] for i in result["items"]]
        self.assertIn("摄像头", names)
        self.assertIn("推送 · wecom_bot", names)

    def test_camera_failure_fails_overall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp),
                                "notify:\n  channels: [wecom_bot]\n")
            result = verify.run_checks(
                root,
                selfcheck_items=lambda: [],
                probe=lambda _p: (False, "设备被占用"),
                notifier=FakeNotifier(),
                ping=lambda *_a, **_k: (True, ""),
            )
        self.assertFalse(result["ok"])
        camera = next(i for i in result["items"] if i["name"] == "摄像头")
        self.assertFalse(camera["ok"])
        self.assertIn("设备被占用", camera["detail"])

    def test_no_channels_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = verify.run_checks(
                root, selfcheck_items=[], probe=lambda _p: (True, ""),
                notifier=FakeNotifier(),
            )
        push = next(i for i in result["items"] if i["name"] == "推送")
        self.assertFalse(push["ok"])
        self.assertIn("没有启用", push["detail"])
        self.assertFalse(result["ok"])

    def test_heartbeat_is_optional_and_absent_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp),
                                "notify:\n  channels: [wecom_bot]\n")
            result = verify.run_checks(
                root, selfcheck_items=[], probe=lambda _p: (True, ""),
                notifier=FakeNotifier(), ping=lambda *_a, **_k: (False, "down"),
            )
        self.assertFalse(any(i["name"] == "外部心跳" for i in result["items"]))
        self.assertTrue(result["ok"])

    def test_heartbeat_failure_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(
                Path(tmp),
                "notify:\n  channels: [wecom_bot]\n"
                "monitor:\n  heartbeat_url: https://hc-ping.com/x\n",
            )
            result = verify.run_checks(
                root, selfcheck_items=[], probe=lambda _p: (True, ""),
                notifier=FakeNotifier(), ping=lambda *_a, **_k: (False, "timeout"),
            )
        hb = next(i for i in result["items"] if i["name"] == "外部心跳")
        self.assertFalse(hb["ok"])
        self.assertFalse(hb["required"])
        self.assertTrue(result["ok"], "心跳失败不该让整体验证失败")

    def test_skipped_camera_is_marked_and_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp),
                                "notify:\n  channels: [wecom_bot]\n")
            result = verify.run_checks(
                root, selfcheck_items=[], include_camera=False,
                notifier=FakeNotifier(),
            )
        camera = next(i for i in result["items"] if i["name"] == "摄像头")
        self.assertTrue(camera["skipped"])
        self.assertTrue(result["ok"])

    def test_json_shape_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            result = verify.run_checks(
                root, selfcheck_items=[], include_push=False,
                include_heartbeat=False, include_camera=False,
            )
        dumped = json.loads(json.dumps(result))
        for key in ("ok", "items", "checked_at", "version"):
            self.assertIn(key, dumped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
