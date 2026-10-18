"""paths.py 的单测：路径基准与解释器发现。

    python tests/test_paths.py

重点不是「函数能跑」，而是两条会静默出错的约定：
1. 相对路径必须落在**项目根**，不是当前工作目录，也不是包目录；
2. 版本号只有一个来源（VERSION 文件），代码里不许再写一份。
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

from roomwatch import __version__, paths


class ResolveTest(unittest.TestCase):
    def test_relative_path_uses_project_root(self) -> None:
        got = paths.resolve("config.yaml")
        self.assertEqual(got, str(paths.PROJECT_ROOT / "config.yaml"))

    def test_absolute_path_passes_through(self) -> None:
        absolute = str(paths.PROJECT_ROOT / "models" / "x.onnx")
        self.assertEqual(paths.resolve(absolute), absolute)

    def test_none_falls_back_to_default(self) -> None:
        self.assertEqual(paths.resolve(None, "data/captures"), str(paths.PROJECT_ROOT / "data" / "captures"))

    def test_none_without_default_is_empty(self) -> None:
        self.assertEqual(paths.resolve(None), "")

    def test_project_root_contains_config(self) -> None:
        self.assertTrue((paths.PROJECT_ROOT / "config.yaml").is_file())

    def test_package_dir_is_inside_root(self) -> None:
        self.assertEqual(paths.PACKAGE_DIR.parent, paths.PROJECT_ROOT)

    def test_runtime_dirs_are_under_root(self) -> None:
        for d in (paths.DATA_DIR, paths.VAR_DIR, paths.CAPTURES_DIR, paths.IDENTITY_DIR,
                  paths.LOG_DIR, paths.STATE_DIR, paths.DB_DIR, paths.MEDIA_DIR, paths.CACHE_DIR):
            self.assertIn(paths.PROJECT_ROOT, Path(d).parents, msg=str(d))


class VersionTest(unittest.TestCase):
    def test_version_file_is_single_source(self) -> None:
        text = (paths.PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(__version__, text)

    def test_version_looks_semver(self) -> None:
        parts = __version__.split(".")
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(p.isdigit() for p in parts[:3]))

    def test_minor_bump_requires_approval(self) -> None:
        """没有所有者批准，只许动修订号。

        VERSION-APPROVED 记的是「已批准到哪一位」（major.minor）。未取得同意时
        VERSION 的前两位不得越过它；越界 = 擅自把测试版升成正式版，测试直接判红。
        """
        approved_raw = (paths.PROJECT_ROOT / "VERSION-APPROVED").read_text(encoding="utf-8")
        approved = [
            ln.strip() for ln in approved_raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ][0]
        cur_major, cur_minor = (int(p) for p in __version__.split(".")[:2])
        ok_major, ok_minor = (int(p) for p in approved.split(".")[:2])
        self.assertLessEqual(
            (cur_major, cur_minor),
            (ok_major, ok_minor),
            msg=(
                f"VERSION={__version__} 超过了已批准基线 {approved}。\n"
                "升次版本号必须先得到项目所有者同意，再把 VERSION-APPROVED 改成新基线；"
                "否则请只改第三位（修订号）。"
            ),
        )


class FindPythonTest(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python.exe"
            exe.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ROOMWATCH_PYTHON": str(exe)}):
                self.assertEqual(paths.find_python(), str(exe))

    def test_falls_back_to_current_interpreter(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROOMWATCH_PYTHON", None)
            self.assertTrue(Path(paths.find_python()).exists())


if __name__ == "__main__":
    unittest.main()
