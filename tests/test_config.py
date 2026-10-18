"""配置加载与本地覆盖的单测。

    python tests/test_config.py

覆盖 `config.local.yaml` 的核心承诺：只写差异就能覆盖主配置，
主配置本身不被修改，相对路径读进来就是绝对路径。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile
import unittest

from roomwatch import config as C

MAIN = """
record:
  max_seconds: 300
  audio:
    enabled: true
    keep_wav: false
snapshot:
  dir: data/captures
notify:
  channel: wxpusher
  title: 房间有人进入
"""

LOCAL = """
notify:
  channel: wecom_bot
  wecom_bot:
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K"
"""


class MergeConfigTest(unittest.TestCase):
    def test_nested_keys_merge_not_replace(self) -> None:
        got = C.merge_config({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
        self.assertEqual(got, {"a": {"x": 1, "y": 9}})

    def test_scalar_override(self) -> None:
        self.assertEqual(C.merge_config({"a": 1}, {"a": 2}), {"a": 2})

    def test_new_keys_added(self) -> None:
        self.assertEqual(C.merge_config({"a": 1}, {"b": 2}), {"a": 1, "b": 2})

    def test_empty_sides(self) -> None:
        self.assertEqual(C.merge_config({}, {"a": 1}), {"a": 1})
        self.assertEqual(C.merge_config({"a": 1}, {}), {"a": 1})
        self.assertEqual(C.merge_config({}, {}), {})

    def test_base_not_mutated(self) -> None:
        base = {"a": {"x": 1}}
        C.merge_config(base, {"a": {"x": 2}})
        self.assertEqual(base, {"a": {"x": 1}}, "纯函数不该改入参")


class LocalPathTest(unittest.TestCase):
    def test_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "config.yaml")
            open(main, "w", encoding="utf-8").close()
            self.assertIsNone(C.local_path_for(main))

    def test_found_next_to_main_config(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            main = os.path.join(d, "config.yaml")
            local = os.path.join(d, "config.local.yaml")
            open(main, "w", encoding="utf-8").close()
            open(local, "w", encoding="utf-8").close()
            self.assertEqual(C.local_path_for(main), local)


class LoadConfigTest(unittest.TestCase):
    def _load(self, with_local: bool) -> dict:
        d = tempfile.mkdtemp()
        main = os.path.join(d, "config.yaml")
        with open(main, "w", encoding="utf-8") as f:
            f.write(MAIN)
        if with_local:
            with open(os.path.join(d, "config.local.yaml"), "w", encoding="utf-8") as f:
                f.write(LOCAL)
        return C.load_config(main)

    def test_main_only(self) -> None:
        cfg = self._load(False)
        self.assertEqual(cfg["notify"]["channel"], "wxpusher")

    def test_local_overrides_channel(self) -> None:
        cfg = self._load(True)
        self.assertEqual(cfg["notify"]["channel"], "wecom_bot")
        self.assertIn("key=K", cfg["notify"]["wecom_bot"]["webhook"])

    def test_local_does_not_clobber_siblings(self) -> None:
        cfg = self._load(True)
        self.assertEqual(cfg["notify"]["title"], "房间有人进入")
        self.assertEqual(cfg["record"]["audio"]["keep_wav"], False)
        self.assertEqual(cfg["record"]["max_seconds"], 300)

    def test_paths_are_absolute(self) -> None:
        cfg = self._load(True)
        self.assertTrue(os.path.isabs(cfg["snapshot"]["dir"]))

    def test_missing_section_is_tolerated(self) -> None:
        d = tempfile.mkdtemp()
        main = os.path.join(d, "config.yaml")
        with open(main, "w", encoding="utf-8") as f:
            f.write("notify:\n  channel: none\n")
        cfg = C.load_config(main)
        self.assertEqual(cfg["notify"]["channel"], "none")

    def test_empty_file_gives_empty_dict(self) -> None:
        d = tempfile.mkdtemp()
        main = os.path.join(d, "config.yaml")
        open(main, "w", encoding="utf-8").close()
        self.assertEqual(C.load_config(main), {})


class WindowsCompatTest(unittest.TestCase):
    def test_normalize_paths_skips_blank_values(self) -> None:
        cfg = {"snapshot": {"dir": ""}, "storage": {"database": None}}
        self.assertEqual(C.normalize_paths(cfg), cfg)


if __name__ == "__main__":
    unittest.main()
