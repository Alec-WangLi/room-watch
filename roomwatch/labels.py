"""身份标签的中文显示。

单独放在这里是为了让打包后的界面能安全导入：它不仅不能依赖 cv2，
连 numpy 也不能碰（`identity.py` 会 import 它们）。
"""

from __future__ import annotations

LABEL_TEXT = {
    "owner": "本人",
    "other": "访客",
    "unknown": "认不出",
}


def identity_text(label: str | None) -> str:
    return LABEL_TEXT.get(str(label or "").strip().lower(), "-")
