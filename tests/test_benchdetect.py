"""推理基准的纯逻辑测试：不加载模型，只验证计时、汇总与失败隔离。

    python tests/test_benchdetect.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import unittest

from roomwatch import benchdetect as B


class SummarizeTest(unittest.TestCase):
    def test_math_is_consistent(self):
        s = B.summarize(frames=30, seconds=1.5)
        self.assertAlmostEqual(s["fps"], 20.0)
        self.assertAlmostEqual(s["ms_per_frame"], 50.0)

    def test_zero_frames_does_not_divide_by_zero(self):
        s = B.summarize(frames=0, seconds=0.0)
        self.assertEqual(s["fps"], 0.0)
        self.assertEqual(s["ms_per_frame"], 0.0)

    def test_cpu_cost_reveals_multithreading(self):
        """墙钟快不代表划算：torch 常靠多核换速度，必须一起看 CPU 时间。"""
        s = B.summarize(frames=10, seconds=1.0, cpu_seconds=6.0)
        self.assertAlmostEqual(s["cpu_ms_per_frame"], 600.0)
        self.assertAlmostEqual(s["cpu_cores"], 6.0)


class FakeDetector:
    name = "fake"

    def __init__(self, detections=1, fail_at=None):
        self.calls = 0
        self.detections = detections
        self.fail_at = fail_at

    def detect(self, frame):
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise RuntimeError("boom")
        return [object()] * self.detections


class BenchDetectorTest(unittest.TestCase):
    def test_warmup_is_excluded_from_timed_frames(self):
        det = FakeDetector(detections=2)
        result = B.bench_detector(det, frames=10, size=(64, 64), warmup=3)
        self.assertEqual(result["ok"], True)
        self.assertEqual(det.calls, 13)          # 3 次预热 + 10 次计时
        self.assertEqual(result["detections"], 20)
        self.assertEqual(result["frames"], 10)

    def test_detector_exception_is_reported_not_raised(self):
        det = FakeDetector(fail_at=2)
        result = B.bench_detector(det, frames=10, size=(64, 64), warmup=0)
        self.assertEqual(result["ok"], False)
        self.assertIn("boom", result["error"])

    def test_json_shape_is_stable(self):
        result = B.bench_detector(FakeDetector(), frames=3, size=(64, 64), warmup=0)
        dumped = json.loads(json.dumps(result))
        for key in ("engine", "ok", "frames", "fps", "ms_per_frame",
                    "detections", "error"):
            self.assertIn(key, dumped)


class EngineAvailabilityTest(unittest.TestCase):
    def test_reports_importable_engines(self):
        available = B.available_engines()
        self.assertIn("cv2", available)
        self.assertIsInstance(available, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
