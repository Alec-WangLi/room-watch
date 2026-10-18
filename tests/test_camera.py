"""采集源解析与重连退避的单测（不碰真实摄像头）。

    python tests/test_camera.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile
import unittest

import cv2

from roomwatch import camera


class ParseSourceTest(unittest.TestCase):
    def test_none_falls_back_to_index(self) -> None:
        spec = camera.parse_source(None, 2)
        self.assertEqual(spec.kind, camera.DEVICE)
        self.assertEqual(spec.value, 2)

    def test_empty_string_falls_back_to_index(self) -> None:
        spec = camera.parse_source("  ", 1)
        self.assertEqual(spec.kind, camera.DEVICE)
        self.assertEqual(spec.value, 1)

    def test_int_and_digit_string_are_devices(self) -> None:
        self.assertEqual(camera.parse_source(0).value, 0)
        self.assertEqual(camera.parse_source("3").value, 3)
        self.assertEqual(camera.parse_source("3").kind, camera.DEVICE)

    def test_rtsp_is_url(self) -> None:
        spec = camera.parse_source("rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101")
        self.assertEqual(spec.kind, camera.URL)
        self.assertEqual(spec.value, "rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101")

    def test_url_label_hides_password(self) -> None:
        spec = camera.parse_source("rtsp://admin:secret@192.168.1.64:554/live")
        self.assertNotIn("secret", spec.label)
        self.assertIn("admin:***@192.168.1.64:554", spec.label)

    def test_mask_url_without_password_unchanged(self) -> None:
        url = "rtsp://192.168.1.64:554/live"
        self.assertEqual(camera.mask_url(url), url)

    def test_existing_file_is_file_source(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            spec = camera.parse_source(path)
            self.assertEqual(spec.kind, camera.FILE)
            self.assertEqual(spec.value, path)
        finally:
            os.remove(path)

    def test_unknown_text_raises_instead_of_guessing(self) -> None:
        with self.assertRaises(ValueError):
            camera.parse_source("D:/not-there/room.mp4")
        with self.assertRaises(ValueError):
            camera.parse_source("rtsp//missing-colon")

    def test_bool_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            camera.parse_source(True)


class BackendTest(unittest.TestCase):
    def test_device_uses_requested_backend(self) -> None:
        spec = camera.parse_source(0)
        self.assertEqual(camera.api_for(spec, "dshow"), cv2.CAP_DSHOW)

    def test_unknown_backend_falls_back_to_dshow(self) -> None:
        spec = camera.parse_source(0)
        self.assertEqual(camera.api_for(spec, "nonsense"), cv2.CAP_DSHOW)

    def test_url_always_uses_ffmpeg(self) -> None:
        spec = camera.parse_source("rtsp://1.2.3.4/live")
        self.assertEqual(camera.api_for(spec, "dshow"), getattr(cv2, "CAP_FFMPEG", cv2.CAP_ANY))


class BackoffTest(unittest.TestCase):
    def test_exponential_growth(self) -> None:
        self.assertEqual(
            [camera.backoff_seconds(n) for n in range(1, 5)],
            [3.0, 6.0, 12.0, 24.0],
        )

    def test_capped(self) -> None:
        self.assertEqual(camera.backoff_seconds(10), 60.0)

    def test_zero_attempt_waits_nothing(self) -> None:
        self.assertEqual(camera.backoff_seconds(0), 0.0)


if __name__ == "__main__":
    unittest.main()
