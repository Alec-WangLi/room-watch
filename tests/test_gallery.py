"""gallery.py 的单测：渲染 + HTTP Range + 目录穿越防护。

    python test_gallery.py

Range 之所以值得专门测：标准库 SimpleHTTPRequestHandler 不支持 Range，
视频能加载但拖不动进度条。这种问题光看页面源码发现不了，必须真的发一个
带 Range 头的请求、看是不是 206 才行。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import contextlib
import io
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from roomwatch import gallery

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


class TestTimestampNames(unittest.TestCase):
    def test_stem_time_label_handles_old_and_new_names(self):
        self.assertEqual(gallery.stem_time_label("094523"), "09:45:23")
        self.assertEqual(gallery.stem_time_label("2026-10-20_094523"), "09:45:23")
        self.assertEqual(gallery.stem_time_label("other"), "other")

    def test_full_timestamp_photo_and_video_pair_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = os.path.join(tmp, "captures")
            day = os.path.join(captures, "2026-10-20")
            os.makedirs(day)
            with open(os.path.join(day, "2026-10-20_094523.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fake-jpeg")
            make_video(os.path.join(day, "2026-10-20_094523.mp4"))
            days = gallery.collect(captures)
        self.assertEqual(len(days), 1)
        img, label, vid = days[0][1][0]
        self.assertEqual(label, "09:45:23")
        self.assertTrue(img.endswith("2026-10-20_094523.jpg"))
        self.assertTrue(vid.endswith("2026-10-20_094523.mp4"))


class TestRangeRequests(GalleryFixture):
    """真的起一个服务打 HTTP——Range 对了才拖得动进度条。"""

    def setUp(self):
        super().setUp()
        make_video(os.path.join(self.day, "090310.mp4"))
        gallery.Handler.captures_dir = os.path.abspath(self.captures)
        gallery.Handler.status_file = os.path.abspath(os.path.join(self.base, "status.json"))
        self.server = gallery.GalleryServer(("127.0.0.1", 0), gallery.Handler)
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


class TestClientDisconnect(GalleryFixture):
    """浏览器掐连接是正常现象，不能变成满屏堆栈。

    真实起因：手动跑 `rw gallery` 挂着两小时，日志里 23 段
    「Exception occurred during processing of request」，全部是
    ConnectionAbortedError(WinError 10053)——浏览器关页面/拖进度条时
    上一条连接就这么断了。原代码只捕 BrokenPipe/ConnectionReset，
    Windows 上这条优雅关闭走的是 ConnectionAborted，于是每次都漏到
    socketserver 打一整套堆栈。
    """

    def setUp(self):
        super().setUp()
        # 文件必须够大（远大于内核发送缓冲），否则服务端一次 write 就写完，
        # 等不到「写着写着对端没了」这个时刻。
        with open(os.path.join(self.day, "100000.mp4"), "wb") as f:
            f.write(b"\0" * (8 * 1024 * 1024))
        gallery.Handler.captures_dir = os.path.abspath(self.captures)
        gallery.Handler.status_file = os.path.abspath(
            os.path.join(self.base, "status.json")
        )
        self.server = gallery.GalleryServer(("127.0.0.1", 0), gallery.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _drop_mid_stream(self, hard_reset: bool) -> str:
        """请求 8MB 视频，读到一半就断，返回服务端这期间打到 stderr 的内容。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            if hard_reset:
                # SO_LINGER(on, 0)：close() 直接发 RST
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            sock.sendall(
                b"GET /captures/2026-10-17/100000.mp4 HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nRange: bytes=0-\r\n"
                b"Connection: close\r\n\r\n"
            )
            sock.recv(2048)  # 确认已经进入正文发送阶段再断
            sock.close()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and "Traceback" not in buf.getvalue():
                time.sleep(0.05)
        return buf.getvalue()

    def _assert_server_alive(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/status")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_connection_aborted_counts_as_client_gone(self):
        """钉死平台差异：Windows 这条断法抛的是 ConnectionAbortedError。"""
        self.assertIn(ConnectionAbortedError, gallery.CLIENT_GONE)

    def test_handle_error_swallows_every_client_gone_flavour(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            for exc_type in (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
                TimeoutError,
            ):
                try:
                    raise exc_type(10053, "对端没了")
                except exc_type:
                    self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(buf.getvalue(), "", "客户端断开不该打堆栈")

    def test_handle_error_still_reports_real_bug(self):
        """吞连接异常不等于吞掉所有异常——真 bug 还必须打印。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                raise ValueError("真的出问题了")
            except ValueError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("ValueError", buf.getvalue())

    def test_graceful_close_mid_stream_leaves_no_traceback(self):
        """真实场景：浏览器关页面（正常 FIN），服务端还在写 → 10053。"""
        noise = self._drop_mid_stream(hard_reset=False)
        self.assertNotIn(
            "Traceback", noise,
            "客户端正常断开被当成服务端故障打印了堆栈：\n" + noise,
        )
        self._assert_server_alive()

    def test_hard_reset_mid_stream_leaves_no_traceback(self):
        """拖进度条：浏览器直接 RST → 10054。"""
        noise = self._drop_mid_stream(hard_reset=True)
        self.assertNotIn(
            "Traceback", noise,
            "客户端 RST 被当成服务端故障打印了堆栈：\n" + noise,
        )
        self._assert_server_alive()


class GalleryPidTest(unittest.TestCase):
    def test_write_and_remove_only_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gallery.pid"
            gallery.write_pid_file(p, 123)
            gallery.remove_pid_file(p, 999)
            self.assertTrue(p.exists())
            gallery.remove_pid_file(p, 123)
            self.assertFalse(p.exists())

    def test_remove_missing_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            gallery.remove_pid_file(Path(tmp) / "nope.pid", 123)


class BindFallbackTest(unittest.TestCase):
    """Windows 可能把端口段保留给系统（WinError 10013）。

    真机事故：8765 落在保留段 8748–8847 里，相册每次启动都直接崩，
    guardian 每分钟重启一次。不能让一个被系统占用的端口把相册彻底钉死。
    """

    def test_preferred_port_used_when_free(self):
        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = address

        with mock.patch.object(gallery, "GalleryServer", FakeServer):
            server, port, reason = gallery.open_server("127.0.0.1", 8765, object())
        self.assertEqual(port, 8765)
        self.assertIsNone(reason)

    def test_reserved_port_falls_back_to_stable_offset(self):
        calls = []

        class FakeServer:
            def __init__(self, address, handler):
                calls.append(address)
                if address[1] == 8765:
                    raise PermissionError(10013, "访问权限不允许")
                self.server_address = address

        with mock.patch.object(gallery, "GalleryServer", FakeServer):
            server, port, reason = gallery.open_server("127.0.0.1", 8765, object())
        self.assertEqual(port, 18765)
        self.assertEqual(calls, [("127.0.0.1", 8765), ("127.0.0.1", 18765)])
        self.assertIsInstance(reason, OSError)

    def test_all_offsets_blocked_falls_back_to_os_assigned(self):
        calls = []

        class FakeServer:
            def __init__(self, address, handler):
                calls.append(address)
                if address[1] != 0:
                    raise PermissionError(10013, "访问权限不允许")
                self.server_address = ("127.0.0.1", 54321)

        with mock.patch.object(gallery, "GalleryServer", FakeServer):
            server, port, reason = gallery.open_server("127.0.0.1", 8765, object())
        self.assertEqual(port, 54321)
        self.assertEqual(
            calls,
            [("127.0.0.1", 8765), ("127.0.0.1", 18765),
             ("127.0.0.1", 28765), ("127.0.0.1", 0)],
        )
        self.assertIsInstance(reason, OSError)

    def test_port_zero_goes_straight_to_os(self):
        calls = []

        class FakeServer:
            def __init__(self, address, handler):
                calls.append(address)
                self.server_address = ("127.0.0.1", 44000)

        with mock.patch.object(gallery, "GalleryServer", FakeServer):
            server, port, reason = gallery.open_server("127.0.0.1", 0, object())
        self.assertEqual(calls, [("127.0.0.1", 0)])
        self.assertEqual(port, 44000)
        self.assertIsNone(reason)


class GalleryStateTest(unittest.TestCase):
    def test_state_records_actual_address_and_is_removed_by_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gallery.json"
            gallery.write_state_file(
                path, pid=123, host="127.0.0.1", port=54321,
                started_at="2026-10-19T15:00:00",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], 123)
            self.assertEqual(data["host"], "127.0.0.1")
            self.assertEqual(data["port"], 54321)
            self.assertEqual(data["url"], "http://127.0.0.1:54321/")

            gallery.remove_state_file(path, 999)
            self.assertTrue(path.exists())
            gallery.remove_state_file(path, 123)
            self.assertFalse(path.exists())

    def test_remove_missing_or_corrupt_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gallery.json"
            gallery.remove_state_file(path, 123)
            path.write_text("not json", encoding="utf-8")
            gallery.remove_state_file(path, 123)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
