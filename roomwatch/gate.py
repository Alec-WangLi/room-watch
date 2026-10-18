"""静止门控：画面没变化就不跑模型。

为什么需要
----------
原来 10fps 每一帧都跑 YOLOv8n，本机实测稳定吃掉 5.9 个核——空房间里没人也照跑。
客户那种 N100 四核小主机直接被打满，表现是掉帧、断流、看起来像「程序不稳定」。

房间场景有个天然特点：**绝大多数时间画面是静的**。先用帧差过一道，静止时
不推理；有人走动才唤醒。省下来的是整块推理算力，不是靠换语言能拿到的。

正确性边界（重要）
------------------
如果人进来后站着不动，帧差会归零，光靠「有变化才推理」会把他判成「没人」，
录像 20 秒后就收尾了。所以这里有三道保险：

1. `busy=True`（正在录像 / 刚检测到人）时**无条件推理**，事件期间一秒都不省；
2. 空闲时也至少每 `force_every_seconds` 强制推理一次，站着不动的人最多 3 秒被发现；
3. 门控只决定「要不要跑模型」，不改变检测结果本身的语义。

代价：静止时 CPU 接近 0，有人时回到正常水平。
"""

from __future__ import annotations

import cv2
import numpy as np


class MotionGate:
    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.width = max(32, int(cfg.get("width", 160)))
        # 灰度差超过这个值才算「变了」。摄像头噪声通常 2~6，12 能滤掉。
        self.diff_threshold = int(cfg.get("diff_threshold", 12))
        # 变化像素占比达到这个值才唤醒。0.4% ≈ 160x90 里 58 个像素。
        self.min_changed_ratio = float(cfg.get("min_changed_ratio", 0.004))
        self.force_every = float(cfg.get("force_every_seconds", 3.0))
        self.idle_grace = float(cfg.get("idle_grace_seconds", 4.0))

        self._prev: np.ndarray | None = None
        self._first = True
        self.last_infer_at = 0.0
        self.inferred = 0
        self.skipped = 0
        self.motion_hits = 0

    def _small_gray(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        scale = self.width / max(1, w)
        size = (self.width, max(1, int(round(h * scale))))
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (5, 5), 0)

    def motion_ratio(self, frame: np.ndarray) -> float:
        """返回变化像素占比 0~1。第一帧（或构图变化）视为全画面变化。"""
        cur = self._small_gray(frame)
        prev, self._prev = self._prev, cur
        if prev is None or prev.shape != cur.shape:
            self._first = True
            return 1.0
        self._first = False
        diff = cv2.absdiff(cur, prev)
        changed = int(np.count_nonzero(diff > self.diff_threshold))
        return changed / float(diff.size)

    def should_infer(self, frame: np.ndarray, now: float, busy: bool = False) -> bool:
        """now 用单调时钟秒（time.monotonic），只用于比较间隔。"""
        ratio = self.motion_ratio(frame)
        first = self._first
        # 第一帧没有参照物，必然「全变」，不该算成一次真实的画面运动，
        # 但必须推理——否则刚启动时画面里有人会被直接跳过。
        moved = ratio >= self.min_changed_ratio and not first
        if moved:
            self.motion_hits += 1

        due = (now - self.last_infer_at) >= self.force_every
        if not self.enabled or busy or first or moved or due:
            self.last_infer_at = now
            self.inferred += 1
            return True

        self.skipped += 1
        return False

    def reset(self) -> None:
        """摄像头重连后画面尺寸可能变了，丢掉旧参考帧。"""
        self._prev = None

    def stats(self) -> dict:
        total = self.inferred + self.skipped
        return {
            "gated": self.enabled,
            "inferred": self.inferred,
            "skipped": self.skipped,
            "motion_hits": self.motion_hits,
            "skip_ratio": round(self.skipped / total, 3) if total else 0.0,
        }
