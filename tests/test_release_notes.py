"""发布脚本的更新简介提取测试：Release 必须带上 CHANGELOG 内容。

    python tests/test_release_notes.py
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import unittest
from pathlib import Path

ROOT = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("rw_release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


class ChangelogNotesTest(unittest.TestCase):
    def test_extracts_version_section(self):
        notes = release.changelog_notes("0.1.19")
        self.assertIn("设置按钮", notes)
        self.assertIn("完整时间戳", notes)
        self.assertNotIn("## [0.1.18]", notes)

    def test_include_adds_unreleased_intermediate_version(self):
        notes = release.changelog_notes("0.1.19", ["0.1.18"])
        self.assertIn("## [0.1.18]", notes)
        self.assertIn("## [0.1.19]", notes)
        self.assertIn("rw verify", notes)

    def test_missing_version_returns_empty(self):
        self.assertEqual(release.changelog_notes("9.9.9"), "")

    def test_build_notes_contains_update_body(self):
        notes = release.build_notes(
            "0.1.19", release.CHANNELS["beta"], "### 更新\n\n- 某处修复", "sha256: abc")
        self.assertIn("测试版", notes)
        self.assertIn("某处修复", notes)
        self.assertIn("sha256: abc", notes)
        self.assertIn("预发布", notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
