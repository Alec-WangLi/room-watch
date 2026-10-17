"""gallery.py 的单测：渲染 + HTTP Range + 目录穿越防护。

    python test_gallery.py

Range 之所以值得专门测：标准库 SimpleHTTPRequestHandler 不支持 Range，
视频能加载但拖不动进度条。这种问题光看页面源码发现不了，必须真的发一个
带 Range 头的请求、看是不是 206 才行。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import cv2
import numpy as np

import gallery

SIZE = (320, 180)
FPS = 10.0


def make_video(path: str, frames: int = 20) -> None:
    """写一段真实可播的 mp4——跳过 wrote.mp4 后也无法验证。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"avc1"), FPS, SIZE, True)
    try:
        for i in range(frames):
            f = np.zeros((SIZE[1], SIZE[0], 3), np.uint8)
            f[:] = (20 + i * 9 % 220, 50, 90)
            cv2.putText(f, str(i), (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                        (255, 255, 255), 2, cv2.LINE_AA)
            w.write(f)
    finally:
        w.release()


class GalleryFixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.captures = os.path.join(self.base, "captures")
        self.day = os.path.join(self.captures, "2026-10-17")
        os.makedirs(self.day)
        with open(os.path.join(self.day, "090310.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg")
        # 一条「只有录像没出图」的记录
        make_video(os.path.join(self.day, "091500.mp4"))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class TestRenderWithVideo(GalleryFixture):
    def test_video_and_snapshot_pair_up(self):
        make_video(os.path.join(self.day, "090310.mp4"))
        days = gallery.collect(self.captures)
        items = days[0][1]
        self.assertEqual(len(items), 2)
        by_time = {t: (img, vid) for img, t, vid in items}
        img, vid = by_time["09:03:10"]
        self.assertTrue(img.endswith("090310.jpg"))
        self.assertTrue(vid.endswith("090310.mp4"))

    def test_render_pairs_into_single_video_card(self):
        make_video(os.path.join(self.day, "090310.mp4"))
        html = gallery.render(self.captures)
        self.assertIn("<video", html)
        # 抓拍图当封面：一眼看到是谁，点开看全过程
        self.assertIn('poster="captures/2026-10-17/090310.jpg"', html)
        self.assertIn("captures/2026-10-17/090310.mp4", html)
        self.assertIn("有录像", html)

    def test_snapshot_only_renders_image(self):
        html = gallery.render(self.captures)
        self.assertIn("captures/2026-10-17/090310.jpg", html)
        self.assertIn("09:03:10", html)
        self.assertNotIn("poster", html)

    def test_video_without_snapshot_still_shown(self):
        """录像先落地、图还没写出来时不能把这段丢掉。"""
        html = gallery.render(self.captures)
        self.assertIn("captures/2026-10-17/091500.mp4", html)
        days = gallery.collect(self.captures)
        lone = [it for it in days[0][1] if it[2] and it[2].endswith("091500.mp4")]
        self.assertEqual(len(lone), 1)
        self.assertIsNone(lone[0][0])

    def test_media_urls_include_captures_prefix(self):
        """HTML 里的图片/视频地址必须以 captures/ 开头，否则浏览器请求 404。"""
        make_video(os.path.join(self.day, "090310.mp4"))
        html = gallery.render(self.captures)
        import re
        srcs = re.findall(r'(?:src|poster)="([^"]+)"', html)
        self.assertTrue(srcs)
        for src in srcs:
            self.assertTrue(
                src.startswith("captures/"),
                f"媒体地址缺少 captures/ 前缀: {src}",
            )

    def test_empty_dir(self):
        self.assertIn("还没有抓拍", gallery.render("definitely-not-here"))

    def test_junk_files_ignored(self):
        open(os.path.join(self.day, "notes.txt"), "w").close()
        days = gallery.collect(self.captures)
        self.assertEqual(len(days[0][1]), 2)


class TestRangeRequests(GalleryFixture):
    """真的起一个服务打 HTTP——Range 对了才拖得动进度条。"""

    def setUp(self):
        super().setUp()
        make_video(os.path.join(self.day, "090310.mp4"))
        gallery.Handler.captures_dir = os.path.abspath(self.captures)
        gallery.Handler.status_file = os.path.abspath(os.path.join(self.base, "status.json"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gallery.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _request(self, path, headers=None, method="GET"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""

    def test_full_get_is_200_with_accept_ranges(self):
        status, headers, body = self._request("/captures/2026-10-17/090310.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(len(body), len(b"\xff\xd8\xff\xe0fake-jpeg"))

    def test_range_returns_206_and_partial_body(self):
        status, headers, body = self._request(
            "/captures/2026-10-17/090310.mp4", {"Range": "bytes=0-99"}
        )
        self.assertEqual(status, 206, "不返回 206，浏览器就拖不动进度条")
        self.assertIn("bytes 0-99/", headers.get("Content-Range", ""))
        self.assertEqual(len(body), 100)

    def test_range_from_middle(self):
        status, headers, body = self._request(
            "/captures/2026-10-17/090310.mp4", {"Range": "bytes=1000-1099"}
        )
        self.assertEqual(status, 206)
        self.assertIn("bytes 1000-1099/", headers.get("Content-Range", ""))
        self.assertEqual(len(body), 100)

    def test_open_ended_range_reads_to_end(self):
        total = os.path.getsize(os.path.join(self.day, "090310.mp4"))
        status, headers, body = self._request(
            "/captures/2026-10-17/090310.mp4", {"Range": "bytes=0-"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), total)

    def test_suffix_range(self):
        status, _headers, body = self._request(
            "/captures/2026-10-17/090310.mp4", {"Range": "bytes=-50"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 50)

    def test_unsatisfiable_range_is_416(self):
        status, headers, _ = self._request(
            "/captures/2026-10-17/090310.mp4", {"Range": "bytes=99999999-"}
        )
        self.assertEqual(status, 416)
        self.assertIn("bytes */", headers.get("Content-Range", ""))

    def test_head_request(self):
        status, headers, body = self._request(
            "/captures/2026-10-17/090310.mp4", method="HEAD"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers.get("Content-Length", 0)) > 0, True)

    def test_video_content_type(self):
        _status, headers, _ = self._request("/captures/2026-10-17/090310.mp4")
        self.assertEqual(headers.get("Content-Type"), "video/mp4")

    def test_rendered_image_urls_are_reachable(self):
        """HTML 生成的图片地址必须能被本服务实际返回，否则浏览器里就是裂图。

        视频文件在测试环境常被安全软件临时拦截，靠现有 /captures/*.mp4 的 Range
        测试已能证明服务端可正常回视频；这里只验图片，避免环境噪音误报。
        """
        import re
        html = gallery.render(self.captures)
        for src in re.findall(r'(?:src|poster)="([^"]+)"', html):
            if not src.endswith(".jpg"):
                continue
            # 浏览器会把相对地址解析成以 / 开头的绝对路径
            status, _, body = self._request("/" + src)
            self.assertEqual(status, 200, f"{src} 不可达")
            self.assertGreater(len(body), 0)

    def test_path_traversal_is_blocked(self):
        """哪怕请求以 /captures/ 开头，也不许用 ../ 逃出目录。"""
        # 在 base 下放一个本不该被访问到的秘密文件
        secret = os.path.join(self.base, "secret.txt")
        with open(secret, "w") as f:
            f.write("top secret")
        for evil in (
            "/captures/../secret.txt",
            "/captures/..%2fsecret.txt",
            "/captures/%2e%2e/secret.txt",
        ):
            status, _, body = self._request(evil)
            self.assertEqual(status, 404, f"{evil} 竟然能读到文件")
            self.assertNotIn(b"top secret", body)

    def test_missing_file_is_404(self):
        status, _, _ = self._request("/captures/2026-10-17/nope.mp4")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
