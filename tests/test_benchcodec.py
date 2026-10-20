"""编码基准的纯逻辑测试：不真的编码，只验证计算、解析与失败隔离。

    python tests/test_benchcodec.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path

from roomwatch import benchcodec as B


class SummarizeTest(unittest.TestCase):
    def test_math_is_consistent(self):
        s = B.summarize("avc1", frames=100, seconds=2.0, size_bytes=10 * 1024 ** 2)
        self.assertAlmostEqual(s["fps"], 50.0)
        self.assertAlmostEqual(s["ms_per_frame"], 20.0)
        self.assertAlmostEqual(s["mb_per_min"], 300.0)
        self.assertAlmostEqual(s["file_mb"], 10.0)
        self.assertTrue(s["ok"])

    def test_zero_seconds_does_not_divide_by_zero(self):
        s = B.summarize("avc1", frames=0, seconds=0.0, size_bytes=0)
        self.assertEqual(s["fps"], 0.0)
        self.assertEqual(s["ms_per_frame"], 0.0)
        self.assertEqual(s["mb_per_min"], 0.0)


class EncoderListTest(unittest.TestCase):
    def test_parses_ffmpeg_encoders_output(self):
        text = (
            "Encoders:\n"
            " V..... = Video\n"
            " ------\n"
            " V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)\n"
            " V....D h264_qsv             H.264 (Intel Quick Sync Video)\n"
            " V....D libx264              libx264 H.264 / AVC (codec h264)\n"
            " A....D aac                  AAC (Advanced Audio Coding)\n"
        )
        names = B.parse_encoder_list(text)
        self.assertIn("h264_nvenc", names)
        self.assertIn("h264_qsv", names)
        self.assertIn("libx264", names)
        self.assertNotIn("Video", names)

    def test_missing_ffmpeg_returns_empty(self):
        def runner(*_a, **_k):
            raise FileNotFoundError("ffmpeg")

        self.assertEqual(B.available_hw_encoders("ffmpeg", runner=runner), [])

    def test_only_known_hardware_encoders_are_returned(self):
        text = " V....D h264_nvenc ...\n V....D libx264 ...\n"
        runner = lambda *a, **k: type(  # noqa: E731
            "R", (), {"stdout": text, "returncode": 0})()
        self.assertEqual(B.available_hw_encoders("ffmpeg", runner=runner),
                         ["h264_nvenc"])


class DefaultWriterFactoryTest(unittest.TestCase):
    def test_opens_a_cv2_writer(self):
        """编码基准的 cv2 路径要和产品共用同一个 writer 实现。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            writer = B._default_writer_factory(
                Path(tmp.name) / "a.mp4", "mp4v", 10.0, (64, 36)
            )
            self.assertIsNotNone(writer)
            writer.release()
        finally:
            tmp.cleanup()


class CandidateListTest(unittest.TestCase):
    def test_candidates_include_libx264(self):
        """libx264 是软件编码基线，必须出现在对比数据里（不进 auto）。"""
        self.assertEqual(
            B.ffmpeg_candidates({"h264_mf", "h264_nvenc"}),
            ["h264_nvenc", "h264_mf", "libx264"],
        )

    def test_candidates_keep_hardware_order(self):
        self.assertEqual(
            B.ffmpeg_candidates({"h264_amf", "h264_qsv"}),
            ["h264_qsv", "h264_amf", "libx264"],
        )


class RunAllTest(unittest.TestCase):
    def test_one_failed_encoder_does_not_abort_others(self):
        tmp = tempfile.TemporaryDirectory()
        calls = []

        def cv2_runner(codec, **_kwargs):
            calls.append(codec)
            if codec == "mp4v":
                raise RuntimeError("boom")
            return B.summarize(codec, frames=10, seconds=0.5, size_bytes=1024)

        def ffmpeg_runner(_encoder, **_kwargs):
            return B.summarize("h264_nvenc", frames=10, seconds=0.4,
                               size_bytes=2048, kind="ffmpeg")

        try:
            results = B.run_all(
                frames=10, size=(64, 32), fps=10, tmpdir=Path(tmp.name),
                cv2_runner=cv2_runner, ffmpeg_bench=ffmpeg_runner,
                hw_encoders=["h264_nvenc"],
            )
        finally:
            tmp.cleanup()

        self.assertEqual(calls, list(B.CV2_CODECS))
        self.assertEqual(len(results), len(B.CV2_CODECS) + 1)
        failed = [r for r in results if not r["ok"]]
        self.assertEqual([r["encoder"] for r in failed], ["mp4v"])
        self.assertIn("boom", failed[0]["error"])
        self.assertTrue(any(r["kind"] == "ffmpeg" for r in results))

    def test_ffmpeg_disabled_only_runs_cv2(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            results = B.run_all(
                frames=1, size=(16, 16), fps=10, tmpdir=Path(tmp.name),
                with_ffmpeg=False,
                cv2_runner=lambda codec, **k: B.summarize(
                    codec, frames=1, seconds=0.1, size_bytes=10),
            )
        finally:
            tmp.cleanup()
        self.assertEqual({r["kind"] for r in results}, {"cv2"})
        self.assertEqual(len(results), len(B.CV2_CODECS))

    def test_json_shape_is_stable(self):
        item = B.summarize("avc1", frames=10, seconds=1.0, size_bytes=1024)
        dumped = json.loads(json.dumps(item))
        for key in ("encoder", "kind", "ok", "fps", "ms_per_frame",
                    "mb_per_min", "file_mb", "error"):
            self.assertIn(key, dumped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
