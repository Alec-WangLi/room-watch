"""自动更新的单测。

    python tests/test_updater.py

重点钉死三件事：
1. 版本纪律：只有同「主.次」的修订号前进才允许自动装，跨次版本必须确认；
2. 下载域名白名单：非 GitHub 域名一律拒绝（更新链路会运行下载来的 exe）；
3. SHA256 校验失败必须判定为失败，不能因为「文件下完了」就放行。

全部用例离线，注入假的 get 函数，不发任何网络请求。
"""

from __future__ import annotations

import os as _os
import sys as _sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from roomwatch import updater


class FakeResponse:
    def __init__(self, status: int = 200, payload=None, text: str = "", body: bytes = b"",
                 headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = {"Content-Length": str(len(body))}
        if headers:
            self.headers.update(headers)
        self._body = body

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def release_payload(tag: str, *, notes: str = "", assets=None, prerelease: bool = False):
    return {
        "tag_name": tag,
        "name": f"room-watch {tag}",
        "body": notes,
        "prerelease": prerelease,
        "published_at": "2026-10-19T00:00:00Z",
        "assets": assets or [],
    }


def installer_asset(version: str = "0.1.4") -> dict:
    return {
        "name": f"room-watch-{version}-setup.exe",
        "size": 149_000_000,
        "browser_download_url":
            f"https://github.com/Alec-WangLi/room-watch/releases/download/v{version}"
            f"/room-watch-{version}-setup.exe",
    }


class VersionTest(unittest.TestCase):
    def test_parse_plain(self) -> None:
        self.assertEqual(updater.Version.parse("0.1.4"), updater.Version(0, 1, 4))

    def test_parse_with_v_prefix(self) -> None:
        self.assertEqual(updater.Version.parse("v1.2.3"), updater.Version(1, 2, 3))

    def test_parse_rejects_garbage(self) -> None:
        with self.assertRaises(updater.UpdateError):
            updater.Version.parse("latest")

    def test_ordering(self) -> None:
        self.assertLess(updater.Version(0, 1, 3), updater.Version(0, 1, 4))
        self.assertLess(updater.Version(0, 1, 9), updater.Version(0, 2, 0))
        self.assertLess(updater.Version(0, 9, 9), updater.Version(1, 0, 0))


class DecideTest(unittest.TestCase):
    """版本纪律的可执行定义。改这里之前先想清楚是不是要放开规则。"""

    def test_same_version_is_up_to_date(self) -> None:
        v = updater.Version(0, 1, 3)
        self.assertEqual(updater.decide(v, v), "up-to-date")

    def test_patch_bump_is_auto(self) -> None:
        self.assertEqual(
            updater.decide(updater.Version(0, 1, 3), updater.Version(0, 1, 4)), "auto")

    def test_minor_bump_requires_confirmation(self) -> None:
        self.assertEqual(
            updater.decide(updater.Version(0, 1, 3), updater.Version(0, 2, 0)),
            "confirm-minor")

    def test_major_bump_requires_confirmation(self) -> None:
        self.assertEqual(
            updater.decide(updater.Version(0, 1, 3), updater.Version(1, 0, 0)),
            "confirm-major")

    def test_downgrade_blocked_by_default(self) -> None:
        self.assertEqual(
            updater.decide(updater.Version(0, 1, 4), updater.Version(0, 1, 3)),
            "blocked-downgrade")

    def test_downgrade_allowed_when_asked(self) -> None:
        self.assertEqual(
            updater.decide(updater.Version(0, 1, 4), updater.Version(0, 1, 3),
                           allow_downgrade=True),
            "downgrade")


class FetchTest(unittest.TestCase):
    def test_fetch_ok(self) -> None:
        def get(url, **kw):
            self.assertIn("api.github.com/repos/Alec-WangLi/room-watch", url)
            return FakeResponse(payload=release_payload("v0.1.4", notes="修了 X"))

        rel = updater.fetch_latest("Alec-WangLi/room-watch", get=get)
        self.assertEqual(rel.version, updater.Version(0, 1, 4))
        self.assertEqual(rel.tag, "v0.1.4")
        self.assertIn("修了 X", rel.notes)

    def test_fetch_404_explains_no_release(self) -> None:
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.fetch_latest("a/b", get=lambda url, **kw: FakeResponse(status=404))
        self.assertIn("release", str(ctx.exception))

    def test_fetch_bad_json(self) -> None:
        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest("a/b", get=lambda url, **kw: FakeResponse(
                payload=ValueError("bad"), text="oops"))

    def test_fetch_network_error(self) -> None:
        def boom(url, **kw):
            raise OSError("no route")

        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest("a/b", get=boom)

    def test_rate_limited_falls_back_to_redirect(self) -> None:
        """匿名 API 有 60 次/小时限制，被限流时必须还能拿到版本号。"""
        calls = []

        def get(url, **kw):
            calls.append(url)
            if "api.github.com" in url:
                return FakeResponse(status=403, text='{"message":"rate limit"}')
            return FakeResponse(status=302, headers={
                "Location": "https://github.com/Alec-WangLi/room-watch/releases/tag/v0.1.4"})

        rel = updater.fetch_latest("Alec-WangLi/room-watch", get=get)
        self.assertEqual(rel.version, updater.Version(0, 1, 4))
        self.assertEqual(rel.tag, "v0.1.4")
        # 回退时没有资产列表，但能按约定拼出安装包地址
        asset = rel.installer_asset()
        self.assertEqual(asset["name"], "room-watch-0.1.4-setup.exe")
        self.assertIn("/releases/download/v0.1.4/", asset["browser_download_url"])
        self.assertEqual(len(calls), 2)

    def test_redirect_without_tag_is_error(self) -> None:
        def get(url, **kw):
            if "api.github.com" in url:
                return FakeResponse(status=429)
            return FakeResponse(status=302, headers={"Location": "https://github.com/login"})

        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest("a/b", get=get)

    def test_redirect_404_means_no_release(self) -> None:
        def get(url, **kw):
            if "api.github.com" in url:
                return FakeResponse(status=403)
            return FakeResponse(status=404)

        with self.assertRaises(updater.UpdateError) as ctx:
            updater.fetch_latest("a/b", get=get)
        self.assertIn("release", str(ctx.exception))


class AssetTest(unittest.TestCase):
    def test_picks_installer(self) -> None:
        rel = updater.Release(
            version=updater.Version(0, 1, 4), tag="v0.1.4", title="", notes="",
            prerelease=False,
            assets=[{"name": "notes.txt"}, installer_asset("0.1.4")],
        )
        self.assertEqual(rel.installer_asset()["name"], "room-watch-0.1.4-setup.exe")

    def test_no_installer(self) -> None:
        rel = updater.Release(version=updater.Version(0, 1, 4), tag="v0.1.4",
                              title="", notes="", prerelease=False, assets=[])
        self.assertIsNone(rel.installer_asset())

    def test_sha_from_release_notes(self) -> None:
        digest = "a" * 64
        rel = updater.Release(
            version=updater.Version(0, 1, 4), tag="v0.1.4", title="", notes=f"SHA256: {digest}",
            prerelease=False, assets=[installer_asset()],
        )
        self.assertEqual(updater.expected_sha256(rel), digest)

    def test_sha_absent(self) -> None:
        rel = updater.Release(
            version=updater.Version(0, 1, 4), tag="v0.1.4", title="", notes="没写摘要",
            prerelease=False, assets=[installer_asset()],
        )
        self.assertIsNone(updater.expected_sha256(rel))


class DownloadGuardTest(unittest.TestCase):
    """下载域名白名单 —— 这是整条链路最需要守住的一条。"""

    def test_rejects_foreign_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(updater.UpdateError) as ctx:
                updater.download("https://evil.example.com/x.exe", Path(tmp) / "x.exe")
        self.assertIn("白名单", str(ctx.exception))

    def test_rejects_http_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(updater.UpdateError):
                updater.download("http://github.com/x.exe", Path(tmp) / "x.exe")

    def test_accepts_github_release_url(self) -> None:
        url = installer_asset()["browser_download_url"]
        self.assertEqual(updater._check_url(url), url)

    def test_download_writes_file(self) -> None:
        body = b"x" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.exe"
            updater.download(installer_asset()["browser_download_url"], dest,
                             get=lambda u, **kw: FakeResponse(body=body))
            self.assertEqual(dest.read_bytes(), body)

    def test_truncated_download_is_rejected(self) -> None:
        class Short(FakeResponse):
            def iter_content(self, chunk_size=1 << 20):
                yield b"only-a-bit"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(updater.UpdateError):
                updater.download(installer_asset()["browser_download_url"],
                                 Path(tmp) / "a.exe", get=lambda u, **kw: Short(body=b"x" * 100))


class Sha256Test(unittest.TestCase):
    def test_verify_ok_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"hello")
            good = updater.sha256_of(p)
            self.assertTrue(updater.verify_sha256(p, good))
            self.assertTrue(updater.verify_sha256(p, good.upper()))
            self.assertFalse(updater.verify_sha256(p, "0" * 64))


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        opt = updater.update_config({})
        self.assertTrue(opt["enabled"])
        self.assertEqual(opt["repo"], updater.DEFAULT_REPO)
        self.assertFalse(opt["allow_minor"])
        self.assertTrue(opt["require_checksum"])

    def test_respects_user_values(self) -> None:
        opt = updater.update_config({"update": {
            "enabled": False, "repo": "me/fork", "allow_minor": True,
            "check_interval_hours": 6,
        }})
        self.assertFalse(opt["enabled"])
        self.assertEqual(opt["repo"], "me/fork")
        self.assertTrue(opt["allow_minor"])
        self.assertEqual(opt["check_interval_hours"], 6)

    def test_garbage_section_is_ignored(self) -> None:
        opt = updater.update_config({"update": "not-a-dict"})
        self.assertTrue(opt["enabled"])


class StopWatcherTest(unittest.TestCase):
    def test_missing_status_file_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(updater.stop_watcher(Path(tmp) / "nope.json"))

    def test_dead_pid_is_false(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "status.json"
            p.write_text(_json.dumps({"pid": 999999}), encoding="utf-8")
            with mock.patch.object(updater, "_pid_alive", return_value=False):
                self.assertFalse(updater.stop_watcher(p))


class CheckCacheTest(unittest.TestCase):
    """界面用的检查结果缓存（var/state/update.json）。"""

    def test_roundtrip(self) -> None:
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(updater.load_check_cache(root), {})
            stamp = datetime.now().isoformat(timespec="seconds")
            updater.save_check_cache({"latest": "0.1.5", "checked_at": stamp}, root)
            self.assertEqual(updater.load_check_cache(root)["latest"], "0.1.5")

    def test_corrupt_cache_is_empty_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = updater.check_cache_path(root)
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(updater.load_check_cache(root), {})
            path.write_text('["a", "b"]', encoding="utf-8")
            self.assertEqual(updater.load_check_cache(root), {})

    def test_missing_timestamp_counts_as_stale(self) -> None:
        self.assertIsNone(updater.check_age_hours({}))
        self.assertTrue(updater.should_auto_check({}, 24))

    def test_zero_interval_disables_auto_check(self) -> None:
        self.assertFalse(updater.should_auto_check({}, 0))

    def test_fresh_cache_skips_auto_check(self) -> None:
        from datetime import datetime

        data = {"checked_at": datetime.now().isoformat(timespec="seconds")}
        self.assertFalse(updater.should_auto_check(data, 24))

    def test_old_cache_triggers_auto_check(self) -> None:
        from datetime import datetime, timedelta

        data = {"checked_at": (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")}
        self.assertTrue(updater.should_auto_check(data, 24))


class AvailableVersionTest(unittest.TestCase):
    def test_newer_patch_is_detected(self) -> None:
        self.assertEqual(str(updater.available_version({"latest": "0.1.5"}, "0.1.4")), "0.1.5")
        self.assertEqual(str(updater.available_version({"latest": "0.2.0"}, "0.1.9")), "0.2.0")

    def test_same_or_older_is_none(self) -> None:
        self.assertIsNone(updater.available_version({"latest": "0.1.4"}, "0.1.4"))
        self.assertIsNone(updater.available_version({"latest": "0.1.3"}, "0.1.4"))

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(updater.available_version({}, "0.1.4"))
        self.assertIsNone(updater.available_version({"latest": "n/a"}, "0.1.4"))


class WaitPidExitTest(unittest.TestCase):
    def test_zero_pid_exits_immediately(self) -> None:
        self.assertTrue(updater.wait_pid_exit(0, timeout=0.5))

    def test_live_pid_times_out(self) -> None:
        import os as _os

        self.assertFalse(updater.wait_pid_exit(_os.getpid(), timeout=0.6))


class GuiLauncherTest(unittest.TestCase):
    def test_missing_root_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(updater.gui_launcher(Path(tmp)))

    def test_prefers_packaged_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "room-watch.exe").write_bytes(b"")
            self.assertEqual(updater.gui_launcher(root), [str(root / "room-watch.exe")])

    def test_falls_back_to_module_without_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text("update: {}\n", encoding="utf-8")
            cmd = updater.gui_launcher(root)
            self.assertTrue(cmd and cmd[-2:] == ["roomwatch", "gui"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
