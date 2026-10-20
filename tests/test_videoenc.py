"""videoenc.py 的单测：编码器解析、探测结果解析与选择顺序。

    python tests/test_videoenc.py

这一层只做「能不能用」的判断，不真正写录像；真编解码在 test_videoenc 的
后端用例和 test_recorder 里覆盖。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import unittest

from roomwatch.videoenc import EncoderSelector, parse_encoders


FFMPEG_ENCODERS = """\
Encoders:
 V..... = Video
 ------
 V....D h264_mf              H264 via MediaFoundation (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
"""


class TestParseEncoders(unittest.TestCase):
    def test_extracts_names_from_ffmpeg_output(self):
        names = parse_encoders(FFMPEG_ENCODERS)
        self.assertIn("h264_mf", names)
        self.assertIn("h264_nvenc", names)
        self.assertIn("libx264", names)
        self.assertNotIn("------", names)
        self.assertNotIn("Encoders:", names)

    def test_empty_input_is_safe(self):
        self.assertEqual(parse_encoders(""), set())
        self.assertEqual(parse_encoders(None), set())


class TestEncoderSelector(unittest.TestCase):
    def _selector(self, cfg=None, available=None, works=None):
        probe_calls = []
        validate_calls = []

        def probe(ffmpeg):
            probe_calls.append(ffmpeg)
            return set(available if available is not None else {"h264_mf", "h264_nvenc"})

        def validate(ffmpeg, encoder):
            validate_calls.append(encoder)
            if works is None:
                return True
            return encoder in works

        sel = EncoderSelector(cfg or {}, probe=probe, validate=validate)
        return sel, probe_calls, validate_calls

    def test_auto_prefers_mf_then_nvenc(self):
        sel, _, _ = self._selector()
        self.assertEqual(sel.choose(), "h264_mf")

    def test_auto_skips_mf_when_validation_fails(self):
        sel, _, _ = self._selector(works={"h264_nvenc"})
        self.assertEqual(sel.choose(), "h264_nvenc")

    def test_auto_returns_none_when_no_hardware_encoder(self):
        """libx264 是软件编码，不参与 auto；没有硬件编码器就用 cv2。"""
        sel, _, _ = self._selector(available={"libx264"})
        self.assertIsNone(sel.choose())

    def test_explicit_encoder_is_tried_first(self):
        sel, _, _ = self._selector(
            {"ffmpeg_encoder": "libx264"},
            available={"h264_mf", "libx264"},
        )
        self.assertEqual(sel.choose(), "libx264")

    def test_explicit_encoder_falls_back_to_auto_order(self):
        sel, _, _ = self._selector(
            {"ffmpeg_encoder": "libx264"},
            available={"h264_mf", "libx264"},
            works={"h264_mf"},
        )
        self.assertEqual(sel.choose(), "h264_mf")

    def test_encoder_cv2_disables_ffmpeg(self):
        sel, probe_calls, _ = self._selector({"encoder": "cv2"})
        self.assertIsNone(sel.choose())
        self.assertEqual(probe_calls, [])

    def test_missing_ffmpeg_returns_none(self):
        def probe(_ffmpeg):
            raise FileNotFoundError("ffmpeg")

        sel = EncoderSelector({}, probe=probe, validate=lambda *_: True)
        self.assertIsNone(sel.choose())

    def test_choice_is_cached(self):
        sel, probe_calls, validate_calls = self._selector()
        self.assertEqual(sel.choose(), "h264_mf")
        self.assertEqual(sel.choose(), "h264_mf")
        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(len(validate_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
