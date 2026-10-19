"""人脸识别纯逻辑测试：不加载模型、不碰摄像头，只锁行为。

真正值得钉死的是三件事：
1. 相似度/平均/分类这些数学定义的边界（一旦搞错，认错人不会有任何报错）
2. 多帧投票：既不能「一帧就认」，也不能「认了又反悔」
3. 多人同框时的合并规则 —— 安全优先，有一张不像你就得算「有他人」
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from roomwatch import identity as I


def unit(v):
    """把一个向量归一化，模拟真实特征。"""
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a)


class TestCosine(unittest.TestCase):
    def test_identical_is_one(self):
        v = unit([1, 2, 3, 4])
        self.assertAlmostEqual(I.cosine_similarity(v, v), 1.0, places=6)

    def test_orthogonal_is_zero(self):
        self.assertAlmostEqual(I.cosine_similarity(unit([1, 0]), unit([0, 1])), 0.0, places=6)

    def test_opposite_is_minus_one(self):
        self.assertAlmostEqual(I.cosine_similarity(unit([1, 0]), unit([-1, 0])), -1.0, places=6)

    def test_zero_vector_is_zero_not_nan(self):
        """零向量会让朴素实现除零出 nan，而 nan 参与比较永远 False，会静默认错人。"""
        self.assertEqual(I.cosine_similarity(np.zeros(8), unit([1] * 8)), 0.0)

    def test_size_mismatch_is_zero(self):
        self.assertEqual(I.cosine_similarity(np.ones(8), np.ones(16)), 0.0)

    def test_scale_invariant(self):
        """特征长度不影响相似度——亮度不同不该改变判定。"""
        v = unit([3, 4, 5])
        self.assertAlmostEqual(I.cosine_similarity(v, v * 100), 1.0, places=6)


class TestAverage(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(I.average_embedding([]))

    def test_all_zero_returns_none(self):
        self.assertIsNone(I.average_embedding([np.zeros(4), np.zeros(4)]))

    def test_single_is_normalized(self):
        out = I.average_embedding([np.array([3.0, 4.0])])
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=6)

    def test_two_identical_keep_direction(self):
        v = unit([0.2, 0.9, -0.3])
        out = I.average_embedding([v, v])
        self.assertGreater(I.cosine_similarity(out, v), 0.999)

    def test_outlier_does_not_flip_template(self):
        """一张极端偏的样本不该把模板拉走——先归一化再平均就是为了这个。"""
        a = unit([1, 0, 0])
        b = unit([0.9, 0.1, 0])
        bad = unit([0, 1, 0]) * 50  # 方向完全不同、模长很大
        out = I.average_embedding([a, b, bad])
        self.assertGreater(I.cosine_similarity(out, a), I.cosine_similarity(out, bad))


class TestClassify(unittest.TestCase):
    def test_equal_to_threshold_is_owner(self):
        self.assertEqual(I.classify(0.45, 0.45), I.LABEL_OWNER)

    def test_just_below_is_other(self):
        self.assertEqual(I.classify(0.4499, 0.45), I.LABEL_OTHER)

    def test_zero_is_other(self):
        self.assertEqual(I.classify(0.0, 0.45), I.LABEL_OTHER)


class TestOwnerTracker(unittest.TestCase):
    def test_nothing_before_confirm_frames(self):
        t = I.OwnerTracker(confirm_frames=3)
        self.assertFalse(t.observe(I.LABEL_OWNER))
        self.assertFalse(t.observe(I.LABEL_OWNER))
        self.assertTrue(t.observe(I.LABEL_OWNER))

    def test_single_frame_is_not_enough(self):
        """单帧命中不能算本人，否则保洁糊一下就免报了。"""
        t = I.OwnerTracker(confirm_frames=2)
        self.assertFalse(t.observe(I.LABEL_OWNER))

    def test_break_resets_streak(self):
        t = I.OwnerTracker(confirm_frames=3)
        t.observe(I.LABEL_OWNER)
        t.observe(I.LABEL_OWNER)
        t.observe(I.LABEL_OTHER)   # 断了
        self.assertFalse(t.observe(I.LABEL_OWNER))
        self.assertFalse(t.owner_present)

    def test_once_confirmed_stays_confirmed_on_single_other(self):
        """认了之后，人低头/转头丢一次脸不该立刻翻脸（由 reset 正式解除）。"""
        t = I.OwnerTracker(confirm_frames=2)
        t.observe(I.LABEL_OWNER)
        t.observe(I.LABEL_OWNER)
        self.assertTrue(t.owner_present)
        t.observe(I.LABEL_OTHER)
        self.assertTrue(t.owner_present)

    def test_reset_clears(self):
        t = I.OwnerTracker(confirm_frames=1)
        t.observe(I.LABEL_OWNER)
        self.assertTrue(t.owner_present)
        t.reset()
        self.assertFalse(t.owner_present)
        self.assertEqual(t.streak, 0)

    def test_unknown_is_not_owner(self):
        t = I.OwnerTracker(confirm_frames=1)
        self.assertFalse(t.observe(I.LABEL_UNKNOWN))


class TestDecide(unittest.TestCase):
    """多人同框的合并规则。用 __new__ 绕过模型加载——这里只验证纯逻辑。"""

    def setUp(self):
        self.idn = I.Identity.__new__(I.Identity)

    def test_empty_is_unknown(self):
        self.assertEqual(self.idn.decide([])[0], I.LABEL_UNKNOWN)

    def test_all_owner(self):
        lab, score = self.idn.decide([(I.LABEL_OWNER, 0.8), (I.LABEL_OWNER, 0.6)])
        self.assertEqual(lab, I.LABEL_OWNER)
        self.assertAlmostEqual(score, 0.8)

    def test_one_other_beats_owners(self):
        """你自己和保洁同框时，不能因为「有主人」就免报——必须按他人处理。"""
        lab, _ = self.idn.decide([(I.LABEL_OWNER, 0.9), (I.LABEL_OTHER, 0.1)])
        self.assertEqual(lab, I.LABEL_OTHER)

    def test_other_score_reports_worst(self):
        """代表分数取最不像你的那张，日志才真实反映风险。"""
        _, score = self.idn.decide([(I.LABEL_OTHER, 0.3), (I.LABEL_OTHER, 0.05)])
        self.assertAlmostEqual(score, 0.05)

    def test_all_unknown(self):
        self.assertEqual(self.idn.decide([(I.LABEL_UNKNOWN, 0.0)])[0], I.LABEL_UNKNOWN)

    def test_owner_beside_unknown_is_owner(self):
        """你在画面里、旁边有个背影（unknown）——仍按本人处理，避免自己刷屏。"""
        lab, _ = self.idn.decide([(I.LABEL_OWNER, 0.7), (I.LABEL_UNKNOWN, 0.0)])
        self.assertEqual(lab, I.LABEL_OWNER)


class TestShiftRow(unittest.TestCase):
    """关键点坐标平移——这是最容易静默出错的一步。"""

    def test_box_and_points_shifted(self):
        from roomwatch.detector import FaceFinder

        row = np.array([10, 20, 30, 40, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 0.99], dtype=np.float32)
        out = FaceFinder._shift_row(row, 100, 200)
        self.assertEqual(out[0], 110)   # x
        self.assertEqual(out[1], 220)   # y
        self.assertEqual(out[2], 30)    # w 不变
        self.assertEqual(out[3], 40)    # h 不变
        self.assertEqual(out[4], 101)   # 第一个关键点 x
        self.assertEqual(out[5], 202)   # 第一个关键点 y
        self.assertEqual(out[12], 109)  # 最后一个关键点 x
        self.assertEqual(out[13], 210)  # 最后一个关键点 y
        self.assertAlmostEqual(float(out[14]), 0.99)  # 置信度不动

    def test_original_row_untouched(self):
        from roomwatch.detector import FaceFinder

        row = np.array([1, 2, 3, 4] + [0] * 10 + [0.9], dtype=np.float32)
        before = row.copy()
        FaceFinder._shift_row(row, 50, 50)
        np.testing.assert_array_equal(row, before)


class TestNoOwnerFile(unittest.TestCase):
    def test_unregistered_is_not_usable(self):
        """没注册模板时必须 unusable，否则会静默把所有访客当本人。"""
        idn = I.Identity({"enabled": True, "owner_file": "identity/__nope__.npz"})
        self.assertFalse(idn.usable)
        self.assertEqual(idn.identify(None, None), (I.LABEL_UNKNOWN, 0.0))


class TestFindFullReturnsRow(unittest.TestCase):
    """回归测试：find_full 必须带回 YuNet 的 15 维关键点行。

    曾经漏了这一步：`row` 恒为 None，注册时表现为「找到 1 张脸，
    但没提取到可用特征」，而 `find()`（只用前 4 个值）依然完全正常。
    也就是说整个识别功能会静默失效，且不报任何错——
    这种 bug 只能靠「真的跑一遍特征提取」来发现。
    """

    IMG = os.path.join("captures", "2026-10-17", "232129.jpg")
    YUNET = os.path.join("models", "face_detection_yunet_2023mar.onnx")
    SFACE = os.path.join("models", "face_recognition_sface_2021dec.onnx")

    def setUp(self):
        import yaml

        from roomwatch.detector import FaceFinder

        self.cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
        self.ff = FaceFinder(self.cfg.get("face") or {})

    @unittest.skipUnless(
        os.path.exists(IMG) and os.path.exists(YUNET), "需要真实抓拍图与 YuNet 模型"
    )
    def test_row_present_and_full_width(self):
        import cv2

        from roomwatch.rules import Detection

        img = cv2.imread(self.IMG)
        h, w = img.shape[:2]
        hits = self.ff.find_full(img, [Detection(0, 0, float(w), float(h), 1.0)])
        self.assertGreaterEqual(len(hits), 1)

        row = hits[0].row
        self.assertIsNotNone(row, "find_full 没有带回关键点行，识别会静默失效")
        self.assertEqual(row.shape, (15,))

        # 坐标必须落在图内 —— 证明裁剪偏移量补对了（补错就会跑到图外）
        self.assertGreaterEqual(row[0], 0)
        self.assertLessEqual(row[0], w)
        for i in range(4, 14, 2):
            self.assertGreaterEqual(row[i], 0)
            self.assertLessEqual(row[i], w)

    @unittest.skipUnless(
        os.path.exists(IMG) and os.path.exists(SFACE), "需要真实抓拍图与 SFace 模型"
    )
    def test_sface_can_actually_embed(self):
        """光有关键点不够，alignCrop + feature 必须真能跑通。"""
        import cv2

        from roomwatch.rules import Detection

        emb = I.FaceEmbedder(self.cfg.get("identity") or {})
        if not emb.enabled:
            self.skipTest("SFace 不可用")

        img = cv2.imread(self.IMG)
        h, w = img.shape[:2]
        hits = self.ff.find_full(img, [Detection(0, 0, float(w), float(h), 1.0)])
        feat = emb.embed(img, hits[0].row)

        self.assertIsNotNone(feat, "提取不到特征，注册和识别都会失效")
        self.assertEqual(feat.shape[0], I.FEATURE_DIM)
        self.assertAlmostEqual(float(np.linalg.norm(feat)), 1.0, places=4)


class TestOwnerPhoto(unittest.TestCase):
    def test_save_photo_and_report_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / "owner.npz"
            np.savez(owner, embedding=np.zeros(I.FEATURE_DIM, dtype=np.float32),
                     samples=np.array(3, dtype=np.int32))
            frame = np.zeros((200, 200, 3), dtype=np.uint8)
            row = np.array([50, 50, 150, 150], dtype=np.float32)
            photo = I.save_owner_photo(str(owner), frame, row)

            self.assertTrue(photo)
            self.assertTrue(Path(photo).is_file())
            info = I.owner_summary(str(owner))
            self.assertEqual(info["photo"], photo)

    def test_clear_removes_template_and_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / "owner.npz"
            owner.write_bytes(b"x")
            photo = I.owner_photo_path(str(owner))
            photo.write_bytes(b"x")

            self.assertTrue(I.clear_owner(str(owner)))
            self.assertFalse(owner.exists())
            self.assertFalse(photo.exists())

    def test_missing_photo_is_reported_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / "owner.npz"
            owner.write_bytes(b"x")
            info = I.owner_summary(str(owner))
        self.assertEqual(info["photo"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
