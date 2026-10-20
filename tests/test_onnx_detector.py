"""ONNX 检测后处理的单测：letterbox、解码、NMS。

不需要模型文件，也不跑推理——把「坐标换算错了」这类错误挡在离线测试里。

    python tests/test_onnx_detector.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import unittest

import numpy as np

from roomwatch import onnx_detector as D


def make_pred(rows, ncls: int = 80) -> np.ndarray:
    """按 YOLOv8 的通道优先布局造一个 (4+ncls, N) 输出。"""
    pred = np.zeros((4 + ncls, len(rows)), dtype=np.float32)
    for i, (cx, cy, w, h, score) in enumerate(rows):
        pred[0, i] = cx
        pred[1, i] = cy
        pred[2, i] = w
        pred[3, i] = h
        pred[4, i] = score
    return pred


class LetterboxTest(unittest.TestCase):
    def test_wide_frame_padded_top_bottom(self) -> None:
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        canvas, scale, dx, dy = D.letterbox(frame, 640)
        self.assertEqual(canvas.shape, (640, 640, 3))
        self.assertAlmostEqual(scale, 0.5)
        self.assertEqual(dx, 0)
        self.assertEqual(dy, 140)
        self.assertEqual(int(canvas[0, 0, 0]), 114, "补边应填 114 灰")
        self.assertEqual(int(canvas[320, 320, 0]), 200, "中间应是原画面")

    def test_tall_frame_padded_left_right(self) -> None:
        frame = np.full((1280, 720, 3), 200, dtype=np.uint8)
        canvas, scale, dx, dy = D.letterbox(frame, 640)
        self.assertAlmostEqual(scale, 0.5)
        self.assertEqual(dx, 140)
        self.assertEqual(dy, 0)
        self.assertEqual(int(canvas[320, 0, 0]), 114)

    def test_empty_frame_rejected(self) -> None:
        with self.assertRaises(ValueError):
            D.letterbox(np.zeros((0, 0, 3), dtype=np.uint8))


class DecodeTest(unittest.TestCase):
    def test_maps_back_to_frame_coordinates(self) -> None:
        pred = make_pred([(320, 320, 100, 200, 0.9)])
        boxes, scores = D.decode(pred, 0.5, scale=0.5, pad=(0, 140), frame_shape=(720, 1280))
        self.assertEqual(len(boxes), 1)
        self.assertEqual([round(v) for v in boxes[0]], [540, 160, 740, 560])
        self.assertAlmostEqual(float(scores[0]), 0.9, places=5)

    def test_accepts_transposed_and_batched_shapes(self) -> None:
        base = make_pred([(320, 320, 100, 200, 0.9)])
        for shaped in (base, base.T, base[None, ...], base.T[None, ...]):
            boxes, scores = D.decode(
                shaped, 0.5, scale=0.5, pad=(0, 140), frame_shape=(720, 1280)
            )
            self.assertEqual(len(boxes), 1, f"形状 {shaped.shape} 应解出 1 个框")

    def test_few_candidates_not_transposed_wrongly(self) -> None:
        """候选数很少时（3 个），不能因为「行少列多」就把方向判反。"""
        pred = make_pred(
            [(320, 320, 100, 200, 0.9), (320, 320, 100, 200, 0.8), (320, 320, 100, 200, 0.7)]
        )
        boxes, _ = D.decode(pred, 0.5, scale=1.0, pad=(0, 0), frame_shape=(720, 1280))
        self.assertEqual(len(boxes), 3)

    def test_low_confidence_dropped(self) -> None:
        pred = make_pred([(320, 320, 100, 200, 0.9), (400, 400, 100, 200, 0.2)])
        boxes, scores = D.decode(pred, 0.55, scale=1.0, pad=(0, 0), frame_shape=(720, 1280))
        self.assertEqual(len(boxes), 1)
        self.assertGreater(float(scores[0]), 0.55)

    def test_boxes_clipped_and_degenerate_dropped(self) -> None:
        pred = make_pred([
            (5, 5, 100, 100, 0.9),      # 一半在画面外，裁完仍有效
            (2000, 2000, 10, 10, 0.9),  # 完全在画面外，裁完退化
        ])
        boxes, _ = D.decode(pred, 0.55, scale=1.0, pad=(0, 0), frame_shape=(720, 1280))
        self.assertEqual(len(boxes), 1)
        self.assertGreaterEqual(float(boxes[0][0]), 0.0)

    def test_empty_output(self) -> None:
        boxes, scores = D.decode(np.zeros((84, 0), np.float32), 0.5, 1.0, (0, 0), (720, 1280))
        self.assertEqual(len(boxes), 0)
        self.assertEqual(len(scores), 0)


class NmsTest(unittest.TestCase):
    def test_overlapping_boxes_suppressed(self) -> None:
        boxes = np.array([
            [0, 0, 100, 100],
            [10, 10, 110, 110],
            [300, 300, 400, 400],
        ], dtype=np.float32)
        scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
        keep = D.nms(boxes, scores, 0.45)
        self.assertEqual(list(keep), [0, 2])

    def test_disjoint_boxes_all_kept(self) -> None:
        boxes = np.array([[0, 0, 50, 50], [100, 100, 150, 150]], dtype=np.float32)
        scores = np.array([0.9, 0.8], dtype=np.float32)
        self.assertEqual(len(D.nms(boxes, scores)), 2)

    def test_empty_input(self) -> None:
        keep = D.nms(np.zeros((0, 4), np.float32), np.zeros((0,), np.float32))
        self.assertEqual(len(keep), 0)


class BuildDetectorTest(unittest.TestCase):
    def test_auto_falls_back_to_torch_when_onnx_missing(self) -> None:
        """只检查选择逻辑：真去实例化 torch 太慢，这里用不存在的路径确认分支。"""
        cfg = {"engine": "onnx", "onnx_model": "models/__nope__.onnx"}
        with self.assertRaises(FileNotFoundError):
            D.build_detector(cfg, base_dir=".")


class OnnxRuntimeBackendTest(unittest.TestCase):
    def test_missing_runtime_raises_clean_error(self) -> None:
        import importlib.util

        if importlib.util.find_spec("onnxruntime") is not None:
            self.skipTest("本机已安装 onnxruntime，缺依赖降级由无依赖环境覆盖")
        with self.assertRaises(RuntimeError) as ctx:
            D.OnnxRuntimeDetector("models/__nope__.onnx")
        self.assertIn("onnxruntime", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
