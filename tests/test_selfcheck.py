"""启动自检的单测。

    python tests/test_selfcheck.py

自检只回答「这台机器上的配置现在能不能跑」，不碰摄像头、不联网。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path

from roomwatch import selfcheck


class SelfcheckTest(unittest.TestCase):
    def test_missing_model_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"detector": {"model": "nope.pt"}, "notify": {"channel": "none"}}
            results = selfcheck.run(cfg, tmp)
            self.assertTrue(any(r.name == "模型" and not r.ok for r in results))

    def test_placeholder_webhook_fails_push(self):
        cfg = {"notify": {"channel": "wecom_bot",
                          "wecom_bot": {"webhook": "https://x?key=REPLACE_ME"}}}
        results = selfcheck.run(cfg, ".")
        push = next(r for r in results if r.name == "推送")
        self.assertFalse(push.ok)

    def test_configured_push_and_models_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "models").mkdir()
            (root / "models" / "y.pt").write_bytes(b"x")
            (root / "models" / "f.onnx").write_bytes(b"x")
            cfg = {"detector": {"model": str(root / "models/y.pt")},
                   "face": {"model": str(root / "models/f.onnx")},
                   "notify": {"channel": "wxpusher",
                              "wxpusher": {"app_token": "AT_REAL", "uid": "UID_REAL"}},
                   "storage": {"min_free_gb": 0}}
            results = selfcheck.run(cfg, root)
            self.assertTrue(all(r.ok for r in results),
                            [r for r in results if not r.ok])

    def test_low_disk_fails(self):
        results = selfcheck.run({}, ".", free_bytes=0)
        self.assertTrue(any(r.name == "磁盘" and not r.ok for r in results))

    def test_result_is_json_serializable(self):
        json.dumps([{"name": r.name, "ok": r.ok, "detail": r.detail}
                    for r in selfcheck.run({}, ".")])

    def test_owner_not_registered_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"identity": {"owner_file": str(Path(tmp) / "missing.npz")}}
            results = selfcheck.run(cfg, tmp)
            model = next(r for r in results if r.name == "模型")
            self.assertTrue(model.ok, model.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
