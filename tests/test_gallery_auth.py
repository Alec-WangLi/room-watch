"""相册鉴权的单测：Basic 解析、绑定校验、真实 HTTP 401/200。

    python tests/test_gallery_auth.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import base64
import http.client
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from roomwatch import gallery

TOKEN = "s3cret-token-1234"


def basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class ParseBasicTest(unittest.TestCase):
    def test_valid_header(self) -> None:
        self.assertEqual(gallery.parse_basic_auth(basic("any", TOKEN)), ("any", TOKEN))

    def test_empty_header(self) -> None:
        self.assertIsNone(gallery.parse_basic_auth(""))

    def test_wrong_scheme(self) -> None:
        self.assertIsNone(gallery.parse_basic_auth("Bearer abc"))

    def test_bad_base64(self) -> None:
        self.assertIsNone(gallery.parse_basic_auth("Basic !!!not-base64!!!"))

    def test_missing_colon(self) -> None:
        raw = base64.b64encode(b"nocolon").decode("ascii")
        self.assertIsNone(gallery.parse_basic_auth("Basic " + raw))


class AuthorizedTest(unittest.TestCase):
    def test_empty_token_disables_auth(self) -> None:
        self.assertTrue(gallery.authorized("", ""))
        self.assertTrue(gallery.authorized("garbage", ""))

    def test_correct_password(self) -> None:
        self.assertTrue(gallery.authorized(basic("any", TOKEN), TOKEN))

    def test_token_in_username_field_also_works(self) -> None:
        self.assertTrue(gallery.authorized(basic(TOKEN, ""), TOKEN))

    def test_wrong_password(self) -> None:
        self.assertFalse(gallery.authorized(basic("any", "wrong"), TOKEN))

    def test_missing_header(self) -> None:
        self.assertFalse(gallery.authorized("", TOKEN))


class BindCheckTest(unittest.TestCase):
    def test_loopback_without_token_is_fine(self) -> None:
        self.assertIsNone(gallery.check_bind("127.0.0.1", ""))
        self.assertIsNone(gallery.check_bind("localhost", ""))

    def test_public_bind_without_token_refused(self) -> None:
        problem = gallery.check_bind("0.0.0.0", "")
        self.assertIsNotNone(problem)
        self.assertIn("拒绝启动", problem)

    def test_public_bind_with_short_token_refused(self) -> None:
        problem = gallery.check_bind("0.0.0.0", "short")
        self.assertIsNotNone(problem)
        self.assertIn("太短", problem)

    def test_public_bind_with_long_token_allowed(self) -> None:
        self.assertIsNone(gallery.check_bind("0.0.0.0", TOKEN))


class HttpAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.captures = os.path.join(self.root, "captures")
        os.makedirs(self.captures, exist_ok=True)
        gallery.Handler.captures_dir = self.captures
        gallery.Handler.status_file = os.path.join(self.root, "status.json")
        gallery.Handler.auth_token = TOKEN
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gallery.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str = "/", auth: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Authorization": auth} if auth else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
        headers_out = dict(resp.getheaders())
        conn.close()
        return status, headers_out, body

    def test_without_credentials_gets_401(self) -> None:
        status, headers, _ = self._get("/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_with_credentials_gets_200(self) -> None:
        status, headers, body = self._get("/", basic("any", TOKEN))
        self.assertEqual(status, 200)
        self.assertIn("房间抓拍", body.decode("utf-8"))

    def test_status_endpoint_also_protected(self) -> None:
        status, _, _ = self._get("/status")
        self.assertEqual(status, 401)
        status, _, _ = self._get("/status", basic("any", TOKEN))
        self.assertEqual(status, 200)

    def test_media_path_also_protected(self) -> None:
        status, _, _ = self._get("/captures/2026-01-01/x.jpg")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
