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

import io
import os
import shutil
import subprocess
import tempfile
import unittest

import cv2
import numpy as np

from roomwatch.videoenc import (
    EncoderSelector,
    FfmpegWriter,
    WriterFactory,
    parse_encoders,
    probe_encoders,
    validate_encoder,
)


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

    def test_exclude_skips_failed_candidate(self):
        sel, _, _ = self._selector()
        self.assertEqual(sel.choose(exclude={"h264_mf"}), "h264_nvenc")


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, buf):
        self.data += buf

    def close(self):
        self.closed = True


class FakeProc:
    """替代 subprocess.Popen：记录写入、可控退出码与超时。"""

    def __init__(self, rc=0, timeout=False, stderr=b""):
        self.stdin = FakeStdin()
        self.stdout = None
        self.stderr = io.BytesIO(stderr)
        self.rc = rc
        self.timeout = timeout
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.timeout and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.rc

    def kill(self):
        self.killed = True

    def poll(self):
        return self.rc


def find_ffmpeg():
    from roomwatch.paths import ffmpeg_exe

    return ffmpeg_exe() or shutil.which("ffmpeg")


def real_encoder(ffmpeg):
    available = probe_encoders(ffmpeg)
    for name in ("h264_mf", "libx264"):
        if name in available:
            return name
    return None


def make_frame(i, size=(128, 72)):
    f = np.zeros((size[1], size[0], 3), np.uint8)
    f[:] = (20 + (i * 11) % 200, 40, 90)
    return f


class ValidateEncoderTest(unittest.TestCase):
    def test_validation_uses_encoder_friendly_size(self):
        """NVENC 在 64×64 上会以 Invalid argument 拒绝；探测必须用能过的尺寸。"""
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            with open(cmd[-1], "wb") as f:
                f.write(b"x")
            return type("R", (), {"returncode": 0})()

        tmp = tempfile.mkdtemp()
        try:
            self.assertTrue(validate_encoder(
                "ffmpeg", "h264_nvenc", runner=runner, tmpdir=tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cmd, kwargs = calls[0]
        self.assertIn("256x144", cmd)
        self.assertEqual(len(kwargs["input"]), 256 * 144 * 3 * 3)


class FfmpegWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "out.mp4")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _writer(self, proc, **kw):
        calls = []

        def spawn(cmd):
            calls.append(cmd)
            return proc

        writer = FfmpegWriter(
            "ffmpeg", "h264_mf", self.path, (640, 360), 10.0, spawn=spawn,
            probe=lambda _path: True, **kw
        )
        return writer, calls

    def test_command_contains_encoder_size_fps_path(self):
        writer, calls = self._writer(FakeProc())
        self.assertTrue(writer.open())
        cmd = calls[0]
        self.assertIn("h264_mf", cmd)
        self.assertIn("640x360", cmd)
        self.assertEqual(cmd[cmd.index("-r") + 1], "10.0")
        self.assertIn(self.path, cmd)
        self.assertIn("+faststart", cmd)

    def test_write_sends_raw_frame_bytes(self):
        proc = FakeProc()
        writer, _ = self._writer(proc)
        writer.open()
        frame = make_frame(1, (640, 360))
        writer.write(frame)
        self.assertEqual(len(proc.stdin.data), frame.nbytes)
        self.assertEqual(writer.written, 1)

    def test_close_ok_returns_true(self):
        proc = FakeProc(rc=0)
        writer, _ = self._writer(proc)
        writer.open()
        writer.write(make_frame(0, (640, 360)))
        with open(self.path, "wb") as f:
            f.write(b"fake mp4")
        self.assertTrue(writer.close())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stderr.closed, "收尾后 stderr 管道要关掉，不能每段泄漏一个句柄")

    def test_close_on_nonzero_rc_deletes_file(self):
        proc = FakeProc(rc=1, stderr=b"encoder failed\n")
        writer, _ = self._writer(proc)
        writer.open()
        with open(self.path, "wb") as f:
            f.write(b"partial")
        self.assertFalse(writer.close())
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNotNone(writer.error)

    def test_close_timeout_kills_process(self):
        proc = FakeProc(rc=0, timeout=True)
        writer, _ = self._writer(proc, timeout=0.1)
        writer.open()
        with open(self.path, "wb") as f:
            f.write(b"fake mp4")
        self.assertFalse(writer.close())
        self.assertTrue(proc.killed)
        self.assertFalse(os.path.exists(self.path))

    def test_zero_byte_file_is_failure(self):
        proc = FakeProc(rc=0)
        writer, _ = self._writer(proc)
        writer.open()
        open(self.path, "wb").close()
        self.assertFalse(writer.close())
        self.assertFalse(os.path.exists(self.path))

    def test_zero_frames_is_failure_even_with_small_file(self):
        """rc=0 但一帧都没写：ffmpeg 会留下几百字节的文件头，这不是录像。"""
        proc = FakeProc(rc=0)
        writer, _ = self._writer(proc)
        writer.open()
        with open(self.path, "wb") as f:
            f.write(b"x" * 261)
        self.assertFalse(writer.close())
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("任何帧", writer.error or "")

    def test_unplayable_file_is_failure(self):
        proc = FakeProc(rc=0)
        writer = FfmpegWriter(
            "ffmpeg", "h264_mf", self.path, (640, 360), 10.0,
            spawn=lambda _cmd: proc, probe=lambda _path: False,
        )
        writer.open()
        writer.write(make_frame(0, (640, 360)))
        with open(self.path, "wb") as f:
            f.write(b"x" * 100)
        self.assertFalse(writer.close())
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("无法播放", writer.error or "")

    def test_real_ffmpeg_roundtrip(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("本机没有 ffmpeg")
        encoder = real_encoder(ffmpeg)
        if not encoder:
            self.skipTest("没有可用的 H.264 编码器")
        writer = FfmpegWriter(ffmpeg, encoder, self.path, (128, 72), 10.0)
        self.assertTrue(writer.open())
        for i in range(10):
            writer.write(make_frame(i))
        self.assertTrue(writer.close(), writer.error)

        cap = cv2.VideoCapture(self.path)
        self.assertTrue(cap.isOpened())
        frames = 0
        while True:
            ok, _ = cap.read()
            if not ok:
                break
            frames += 1
        cap.release()
        self.assertGreater(frames, 0)

    def test_single_frame_segment_is_valid(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("本机没有 ffmpeg")
        encoder = real_encoder(ffmpeg)
        if not encoder:
            self.skipTest("没有可用的 H.264 编码器")
        writer = FfmpegWriter(ffmpeg, encoder, self.path, (128, 72), 10.0)
        writer.open()
        writer.write(make_frame(0))
        self.assertTrue(writer.close(), writer.error)
        cap = cv2.VideoCapture(self.path)
        self.assertTrue(cap.isOpened())
        ok, _ = cap.read()
        cap.release()
        self.assertTrue(ok, "只有一帧的片段也必须能读出来")


class FakeWriter:
    created = 0
    open_result = True

    def __init__(self, *args, **kwargs):
        type(self).created += 1
        self.args = args
        self.path = args[2] if len(args) > 2 else "x.mp4"
        self.description = "fake"
        self.error = "boom"
        self.opened = False

    def open(self):
        self.opened = True
        return type(self).open_result

    def write(self, frame):
        pass

    def close(self):
        return True


class FakeCv2Writer(FakeWriter):
    def __init__(self, path, size, fps, codecs=None):
        super().__init__(path, size, fps)
        self.description = "avc1"


class FakeFfmpegWriter(FakeWriter):
    def __init__(self, ffmpeg, encoder, path, size, fps, **_kw):
        super().__init__(ffmpeg, encoder, path, size, fps)
        self.description = encoder


class FakeSelector:
    def __init__(self, choice):
        self.choice = choice

    def choose(self, exclude=()):
        return self.choice


class ScriptedSelector:
    def __init__(self, names):
        self.names = list(names)
        self.calls = []

    def choose(self, exclude=()):
        self.calls.append(tuple(sorted(exclude)))
        for name in self.names:
            if name not in exclude:
                return name
        return None


class TestWriterFactory(unittest.TestCase):
    def setUp(self):
        FakeFfmpegWriter.created = 0
        FakeFfmpegWriter.open_result = True

    def _factory(self, choice, **kw):
        cfg = {"encoder": "auto", "codec": "avc1", "audio": {"ffmpeg": "ffmpeg"}}
        cfg.update(kw)
        return WriterFactory(
            cfg,
            selector=FakeSelector(choice),
            cv2_factory=FakeCv2Writer,
            ffmpeg_factory=FakeFfmpegWriter,
        )

    def test_no_ffmpeg_encoder_uses_cv2(self):
        factory = self._factory(None)
        self.assertIsInstance(factory.open("a.mp4", (640, 360), 10.0), FakeCv2Writer)
        self.assertIn("ffmpeg", factory.disabled_reason or "",
                      "回退 cv2 时要说明为什么没用 ffmpeg")
        factory.note_segment_ok()
        self.assertIn("ffmpeg", factory.disabled_reason or "",
                      "成功录了 cv2 也不能把「没有 ffmpeg」的原因抹掉")

    def test_ffmpeg_writer_is_used_when_available(self):
        FakeFfmpegWriter.created = 0
        FakeFfmpegWriter.open_result = True
        factory = self._factory("h264_mf")
        writer = factory.open("a.mp4", (640, 360), 10.0)
        self.assertIsInstance(writer, FakeFfmpegWriter)
        self.assertEqual(FakeFfmpegWriter.created, 1)

    def test_open_failure_falls_back_to_cv2(self):
        FakeFfmpegWriter.open_result = False
        factory = self._factory("h264_mf")
        writer = factory.open("a.mp4", (640, 360), 10.0)
        self.assertIsInstance(writer, FakeCv2Writer)
        self.assertTrue(factory.disabled_reason)

    def test_circuit_breaker_after_two_failures(self):
        FakeFfmpegWriter.created = 0
        FakeFfmpegWriter.open_result = False
        factory = self._factory("h264_mf")
        for _ in range(3):
            factory.open("a.mp4", (640, 360), 10.0)
        self.assertEqual(FakeFfmpegWriter.created, 2,
                         "连续两次失败后不该再启动 ffmpeg")
        self.assertIn("连续", factory.disabled_reason or "")

    def test_failed_segment_rejects_encoder_for_next_segment(self):
        selector = ScriptedSelector(["h264_mf", "h264_nvenc"])
        cfg = {"encoder": "auto", "codec": "avc1", "audio": {"ffmpeg": "ffmpeg"}}
        factory = WriterFactory(
            cfg, selector=selector,
            cv2_factory=FakeCv2Writer, ffmpeg_factory=FakeFfmpegWriter,
        )
        FakeFfmpegWriter.open_result = False
        first = factory.open("a.mp4", (640, 360), 10.0)
        self.assertIsInstance(first, FakeCv2Writer)
        FakeFfmpegWriter.open_result = True
        second = factory.open("b.mp4", (640, 360), 10.0)
        self.assertIsInstance(second, FakeFfmpegWriter)
        self.assertEqual(second.description, "h264_nvenc",
                         "下一段要跳过刚失败的 h264_mf")
        self.assertEqual(selector.calls[-1], ("h264_mf",))

    def test_breaker_resets_after_success(self):
        selector = ScriptedSelector(["h264_mf", "h264_nvenc"])
        cfg = {"encoder": "auto", "codec": "avc1", "audio": {"ffmpeg": "ffmpeg"}}
        factory = WriterFactory(
            cfg, selector=selector,
            cv2_factory=FakeCv2Writer, ffmpeg_factory=FakeFfmpegWriter,
        )
        FakeFfmpegWriter.open_result = False
        factory.open("a.mp4", (640, 360), 10.0)  # h264_mf 失败
        FakeFfmpegWriter.open_result = True
        self.assertIsInstance(factory.open("b.mp4", (640, 360), 10.0),
                              FakeFfmpegWriter)  # h264_nvenc 成功
        factory.note_segment_ok()
        self.assertFalse(factory._ffmpeg_off)
        self.assertEqual(factory._failures, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
