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
from roomwatch.rules import Detection


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


class PercentileTest(unittest.TestCase):
    def test_p50_p95_use_linear_interpolation(self):
        values = list(range(1, 101))
        self.assertAlmostEqual(B.percentile(values, 50), 50.5, places=2)
        self.assertAlmostEqual(B.percentile(values, 95), 95.05, places=2)

    def test_empty_and_single_value(self):
        self.assertEqual(B.percentile([], 95), 0.0)
        self.assertAlmostEqual(B.percentile([7.2], 95), 7.2)


class DetectionMatchTest(unittest.TestCase):
    def _det(self, x1, y1, x2, y2, conf):
        return Detection(x1=x1, y1=y1, x2=x2, y2=y2, conf=conf)

    def test_identical_detections_match(self):
        a = [self._det(10, 10, 110, 110, 0.9)]
        self.assertTrue(B.detections_match(a, list(a)))

    def test_conf_drift_within_tolerance(self):
        a = [self._det(10, 10, 110, 110, 0.90)]
        b = [self._det(10, 10, 110, 110, 0.91)]
        self.assertTrue(B.detections_match(a, b))

    def test_missing_box_is_rejected(self):
        a = [self._det(10, 10, 110, 110, 0.9)]
        b = a + [self._det(200, 200, 300, 300, 0.8)]
        self.assertFalse(B.detections_match(a, b))

    def test_low_iou_is_rejected(self):
        a = [self._det(10, 10, 110, 110, 0.9)]
        b = [self._det(30, 30, 130, 130, 0.9)]
        self.assertFalse(B.detections_match(a, b))


class EngineSettingTest(unittest.TestCase):
    def test_cv2_maps_to_existing_onnx_engine(self):
        self.assertEqual(B.engine_setting("cv2"), "onnx")
        self.assertEqual(B.engine_setting("onnxruntime"), "onnxruntime")

    def test_auto_keeps_detector_default(self):
        self.assertIsNone(B.engine_setting("auto"))

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            B.engine_setting("nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
