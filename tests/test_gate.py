"""静止门控的单测（合成画面，不依赖摄像头）。

    python tests/test_gate.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import unittest

import cv2
import numpy as np

from roomwatch.gate import MotionGate


def blank(h: int = 90, w: int = 160) -> np.ndarray:
    return np.full((h, w, 3), 40, dtype=np.uint8)


def with_block(h: int = 90, w: int = 160, value: int = 200) -> np.ndarray:
    frame = blank(h, w)
    cv2.rectangle(frame, (20, 20), (80, 70), (value, value, value), -1)
    return frame


class GateTest(unittest.TestCase):
    def test_first_frame_always_infers(self) -> None:
        gate = MotionGate({})
        self.assertTrue(gate.should_infer(blank(), 0.0))

    def test_static_scene_is_skipped(self) -> None:
        gate = MotionGate({})
        frame = blank()
        self.assertTrue(gate.should_infer(frame, 0.0))
        self.assertFalse(gate.should_infer(frame, 0.1))
        self.assertFalse(gate.should_infer(frame, 0.2))
        self.assertEqual(gate.stats()["skipped"], 2)

    def test_motion_wakes_it_up(self) -> None:
        gate = MotionGate({})
        gate.should_infer(blank(), 0.0)
        self.assertTrue(gate.should_infer(with_block(), 0.1))
        self.assertEqual(gate.stats()["motion_hits"], 1)

    def test_force_interval_beats_static_scene(self) -> None:
        gate = MotionGate({"force_every_seconds": 3.0})
        frame = blank()
        gate.should_infer(frame, 0.0)
        self.assertFalse(gate.should_infer(frame, 1.0))
        self.assertTrue(gate.should_infer(frame, 3.1), "再静也要按最长间隔强制看一眼")

    def test_busy_bypasses_gate(self) -> None:
        gate = MotionGate({"force_every_seconds": 100.0})
        frame = blank()
        gate.should_infer(frame, 0.0)
        self.assertTrue(gate.should_infer(frame, 0.1, busy=True))

    def test_disabled_always_infers(self) -> None:
        gate = MotionGate({"enabled": False})
        frame = blank()
        for t in (0.0, 0.1, 0.2):
            self.assertTrue(gate.should_infer(frame, t))

    def test_reset_clears_reference_frame(self) -> None:
        gate = MotionGate({})
        frame = blank()
        gate.should_infer(frame, 0.0)
        gate.reset()
        self.assertTrue(gate.should_infer(frame, 0.1), "重连后第一帧必须重新推理")

    def test_skip_ratio_reported(self) -> None:
        gate = MotionGate({"force_every_seconds": 100.0})
        frame = blank()
        gate.should_infer(frame, 0.0)
        for i in range(9):
            gate.should_infer(frame, 0.1 + i)
        self.assertAlmostEqual(gate.stats()["skip_ratio"], 0.9, places=3)


if __name__ == "__main__":
    unittest.main()
