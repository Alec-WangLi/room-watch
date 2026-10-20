"""自动更新：查 GitHub Releases → 比对版本 → 下载校验 → 静默安装。

设计上的几条硬约束
------------------
1. **只认 GitHub 上的 release 资产**，下载域名白名单写死；不跟随任意重定向。
2. **必须校验 SHA256**（优先取 `*.sha256` 资产，其次 release 说明里的
   `sha256: <64 位>`）。校验不过一律不装 —— 更新链路是整条软件里唯一
   「从网络下载可执行文件并运行」的地方，不能图省事。
3. **遵守版本纪律**（项目所有者定的规则）：同一 `主.次` 下只升修订号时
   自动安装；跨次版本属于「正式版升级」，必须显式确认；跨主版本同理；
   降级默认拒绝。这条不是技术限制，是产品规则，写在这里避免以后被绕过。
4. **Windows 才装安装包**。Linux/源码部署走 `git pull`，这里只提示。

安装器行为：调用 `setup.exe /VERYSILENT`，它内部会用同一 AppId 先跑旧版
卸载器再装新版；卸载器已刻意保留 config.local.yaml 与 data/，所以升级不丢配置。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import control
from . import paths
from . import procutil

DEFAULT_REPO = "Alec-WangLi/room-watch"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
API_RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=30"
API_RELEASE_BY_TAG = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
ATOM_FEED = "https://github.com/{repo}/releases.atom"
LATEST_PAGE = "https://github.com/{repo}/releases/latest"
ASSET_URL = "https://github.com/{repo}/releases/download/{tag}/{name}"

# 只允许从这些域下载。GitHub 的资产会 302 到 objects.githubusercontent.com，
# 所以它必须在名单里；除此之外的域名一律拒绝，避免被中间人引到别处。
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
})

INSTALLER_RE = re.compile(r"room-watch-(?P<ver>\d+\.\d+\.\d+)-setup\.exe$", re.IGNORECASE)
SHA256_ASSET_RE = re.compile(r"\.exe\.sha256$", re.IGNORECASE)
SHA256_IN_TEXT_RE = re.compile(r"sha256[:\s=]+([0-9a-fA-F]{64})", re.IGNORECASE)
TAG_VER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

USER_AGENT = "room-watch-updater/1"


class UpdateError(RuntimeError):
    """更新流程中的可预期失败（网络、格式、校验），message 直接给用户看。"""


# ---------------------------------------------------------------- 版本


@dataclass(frozen=True, order=True)
class Version:
    """语义化版本。只比较数字三元组，忽略 `-rc1` 这类后缀（release 会另行标记）。"""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = TAG_VER_RE.search(str(text or ""))
        if not m:
            raise UpdateError(f"无法识别的版本号：{text!r}")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    @property
    def base(self) -> tuple[int, int]:
        """主.次 —— 版本纪律里的「已批准基线」就是比这一对。"""
        return (self.major, self.minor)

    def __str__(self) -> str:  # pragma: no cover - 平凡
        return f"{self.major}.{self.minor}.{self.patch}"


def decide(current: Version, latest: Version, *, allow_downgrade: bool = False) -> str:
    """返回处置策略，取值固定为下面几种，方便单测与调用方分支。

    - ``up-to-date``        已经是最新
    - ``auto``              同一主.次，只有修订号前进 → 自动装
    - ``confirm-minor``     次版本前进 → 需确认（版本纪律：正式版升级）
    - ``confirm-major``     主版本前进 → 需确认
    - ``downgrade``         远端比本地旧
    - ``blocked-downgrade`` 远端更旧且不允许降级
    """
    if latest == current:
        return "up-to-date"
    if latest < current:
        return "downgrade" if allow_downgrade else "blocked-downgrade"
    if latest.base == current.base:
        return "auto"
    if latest.major != current.major:
        return "confirm-major"
    return "confirm-minor"


# ---------------------------------------------------------------- 远端数据


@dataclass
class Release:
    version: Version
    tag: str
    title: str
    notes: str
    prerelease: bool
    assets: list[dict] = field(default_factory=list)
    published_at: str = ""
    repo: str = ""

    def installer_asset(self) -> Optional[dict]:
        for a in self.assets:
            if INSTALLER_RE.search(a.get("name", "")):
                return a
        # 走「重定向模式」时拿不到资产列表，按 GitHub 的固定路径约定拼出来
        if self.repo and self.tag:
            name = f"room-watch-{self.version}-setup.exe"
            return {
                "name": name,
                "size": 0,
                "browser_download_url": ASSET_URL.format(
                    repo=self.repo, tag=self.tag, name=name),
            }
        return None


def _check_url(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UpdateError(f"拒绝从非白名单域名下载：{host or url}")
    if urlparse(url).scheme not in ("https", ""):
        raise UpdateError(f"只接受 https 下载：{url}")
    return url


def _default_get(timeout: float = 10.0, **extra):
    import requests

    def get(url, **kw):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        headers.update(kw.pop("headers", {}) or {})
        return requests.get(url, timeout=timeout, headers=headers, **kw)

    return get


def _release_from_api(data: dict, repo: str) -> Optional[Release]:
    """把 GitHub API 的一个 release 对象转成 Release；tag 不合法时返回 None。"""
    tag = str(data.get("tag_name") or "")
    try:
        version = Version.parse(tag)
    except UpdateError:
        return None
    return Release(
        version=version,
        tag=tag,
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        prerelease=bool(data.get("prerelease")),
        assets=list(data.get("assets") or []),
        published_at=str(data.get("published_at") or ""),
        repo=repo,
    )


def _fetch_beta_via_atom(repo: str, *, timeout: float = 10.0,
                         get: Optional[Callable[..., object]] = None) -> Release:
    """API 被限流时，从 `releases.atom` 取版本列表。

    网页的 atom 源不消耗 API 配额，且包含预发布（实测 v0.1.19-beta 就在里面）。
    代价是拿不到简介和资产列表：安装包地址按固定命名约定拼，SHA256 仍会
    从 `.sha256` 资产下载校验。
    """
    get = get or _default_get(timeout)
    url = ATOM_FEED.format(repo=repo)
    try:
        resp = get(url, headers={"Accept": "application/atom+xml"})
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"测试版通道：API 被限流，atom 源也连不上（{exc}）") from exc
    status = getattr(resp, "status_code", 0)
    if status != 200:
        raise UpdateError(f"测试版通道：API 被限流，atom 源返回 HTTP {status}")
    try:
        root = ET.fromstring(getattr(resp, "text", "") or "")
    except ET.ParseError as exc:
        raise UpdateError("测试版通道：atom 源内容无法解析") from exc

    ns = {"a": "http://www.w3.org/2005/Atom"}
    candidates: list[Release] = []
    for entry in root.findall("a:entry", ns):
        href = ""
        for link in entry.findall("a:link", ns):
            href = str(link.get("href") or "")
            if "/releases/tag/" in href:
                break
        if "/releases/tag/" not in href:
            continue
        tag = href.rstrip("/").rsplit("/", 1)[-1]
        try:
            version = Version.parse(tag)
        except UpdateError:
            continue
        candidates.append(Release(
            version=version,
            tag=tag,
            title=str(entry.findtext("a:title", default=tag, namespaces=ns) or tag),
            notes="",
            prerelease=("-" in tag),
            assets=[],
            published_at=str(entry.findtext("a:updated", default="", namespaces=ns) or ""),
            repo=repo,
        ))
    if not candidates:
        raise UpdateError("测试版通道：atom 源里没有可识别的 release")
    return max(candidates, key=lambda r: (r.version, 0 if not r.prerelease else -1))


def fetch_channel(repo: str = DEFAULT_REPO, channel: str = "stable", *,
                  timeout: float = 10.0,
                  get: Optional[Callable[..., object]] = None) -> Release:
    """按更新通道取版本。

    - stable：走 `/releases/latest`，GitHub 自己排除预发布。
    - beta：读 `/releases` 列表，把预发布也算进来，挑版本最高的一个；
      同一版本同时存在稳定版与测试版时，优先稳定版。
      beta 通道必须走 API（网页 `/releases/latest` 不返回预发布），
      被限流时无法退回重定向，只能提示稍后再试。
    """
    if str(channel or "stable").strip().lower() != "beta":
        return fetch_latest(repo, timeout=timeout, get=get)

    get = get or _default_get(timeout)
    url = API_RELEASES.format(repo=repo)
    try:
        resp = get(url)
    except Exception as exc:  # noqa: BLE001 - 网络异常先尝试 atom 兜底
        try:
            return _fetch_beta_via_atom(repo, timeout=timeout, get=get)
        except UpdateError as atom_exc:
            raise UpdateError(f"连不上 GitHub：{exc}；{atom_exc}") from exc

    status = getattr(resp, "status_code", 0)
    if status in (403, 429):
        # 匿名 API 每小时只有 60 次；atom 源不耗配额，且包含预发布。
        return _fetch_beta_via_atom(repo, timeout=timeout, get=get)
    if status != 200:
        raise UpdateError(f"GitHub 返回 {status}：{(getattr(resp, 'text', '') or '')[:200]}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub 返回的 releases 不是有效 JSON") from exc
    if not isinstance(data, list):
        raise UpdateError("GitHub 返回的 releases 格式不对")

    candidates: list[Release] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        release = _release_from_api(item, repo)
        if release is None:
            continue
        # 列表模式必须确认真的有安装包资产；不能像重定向模式那样按名字拼。
        if not any(INSTALLER_RE.search(str(a.get("name") or ""))
                   for a in release.assets):
            continue
        candidates.append(release)

    if not candidates:
        raise UpdateError("beta 通道没有可用的安装包 release")
    return max(candidates, key=lambda r: (r.version, 0 if not r.prerelease else -1))


def fetch_latest(repo: str = DEFAULT_REPO, *, timeout: float = 10.0,
                 get: Optional[Callable[..., object]] = None) -> Release:
    """取最新 release。

    先走 API（能拿到说明和资产列表），**遇到限流就退回网页重定向**：
    匿名请求每小时只有 60 次，共享出口 IP 很容易被打满（实测遇到过 403）。
    重定向模式只拿得到版本号，资产按 GitHub 的固定路径拼，足够完成更新。
    """
    get = get or _default_get(timeout)
    url = API_LATEST.format(repo=repo)

    status = 0
    try:
        resp = get(url)
        status = getattr(resp, "status_code", 0)
    except Exception as exc:  # requests 的异常类型多，统一收口
        resp = None
        status = 0
        api_error = f"连不上 GitHub：{exc}"
    else:
        api_error = ""

    if status == 200:
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            release = _release_from_api(data, repo)
            if release is not None:
                return release

    if status == 404:
        raise UpdateError(f"仓库 {repo} 还没有发布任何 release")
    if status in (0, 403, 429) or api_error:
        # 限流 / 网络不可达：退回不消耗配额的网页重定向
        return _fetch_via_redirect(repo, get=get, fallback_reason=api_error or f"HTTP {status}")
    raise UpdateError(f"GitHub 返回 {status}：{(getattr(resp, 'text', '') or '')[:200]}")


def _fetch_via_redirect(repo: str, *, get: Callable[..., object],
                        fallback_reason: str = "") -> Release:
    """`/releases/latest` 会 302 到 `/releases/tag/vX.Y.Z`，从 Location 里取 tag。

    这条路径不经过 api.github.com，因此不受 60 次/小时限制。
    """
    page = LATEST_PAGE.format(repo=repo)
    try:
        resp = get(page, allow_redirects=False)
    except Exception as exc:
        hint = f"（API 失败原因：{fallback_reason}）" if fallback_reason else ""
        raise UpdateError(f"连不上 GitHub{hint}：{exc}") from exc

    status = getattr(resp, "status_code", 0)
    loc = str((getattr(resp, "headers", {}) or {}).get("Location") or "")

    if status in (301, 302, 303, 307, 308) and "/tag/" in loc:
        tag = loc.rstrip("/").rsplit("/", 1)[-1]
        return Release(
            version=Version.parse(tag), tag=tag, title=tag, notes="",
            prerelease=False, assets=[], repo=repo,
        )
    if status == 404:
        raise UpdateError(f"仓库 {repo} 还没有发布任何 release")
    raise UpdateError(
        f"取最新版本失败（HTTP {status}），也没能从重定向里解析出 tag。"
        + (f" API 侧：{fallback_reason}" if fallback_reason else "")
    )


def fetch_sha256_asset(release: Release, *, get: Optional[Callable[..., object]] = None,
                       timeout: float = 30.0) -> Optional[str]:
    """下载 `<安装包>.sha256` 并解析摘要。没有该资产时返回 None。"""
    installer = release.installer_asset()
    if not installer or not release.repo:
        return None
    name = installer["name"] + ".sha256"
    url = ASSET_URL.format(repo=release.repo, tag=release.tag, name=name)
    get = get or _default_get(timeout)
    try:
        resp = get(url, allow_redirects=True)
    except Exception:
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    m = SHA256_IN_TEXT_RE.search(str(getattr(resp, "text", "") or ""))
    if m:
        return m.group(1).lower()
    # 有的 .sha256 文件只有裸摘要，没有 "sha256:" 前缀
    raw = (getattr(resp, "text", "") or "").strip().split()
    for token in raw:
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    return None


def expected_sha256(release: Release) -> Optional[str]:
    """找安装包的期望摘要：先看 `*.sha256` 资产，再看 release 说明。"""
    installer = release.installer_asset()
    if not installer:
        return None
    for a in release.assets:
        if SHA256_ASSET_RE.search(a.get("name", "")):
            return None  # 需要下载该资产才能读到，交给调用方处理
    m = SHA256_IN_TEXT_RE.search(release.notes or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------- 下载与校验


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, timeout: float = 120.0,
             progress: Optional[Callable[[int, int], None]] = None,
             get: Optional[Callable[..., object]] = None) -> Path:
    """流式下载到 dest（先写 .part 再改名，断点残留不会污染缓存）。"""
    _check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if get is None:
        import requests

        def get(u, **kw):  # type: ignore[misc]
            return requests.get(u, timeout=timeout, stream=True,
                                headers={"User-Agent": USER_AGENT})

    try:
        resp = get(url)
    except Exception as exc:
        raise UpdateError(f"下载失败：{exc}") from exc

    status = getattr(resp, "status_code", 0)
    if status != 200:
        raise UpdateError(f"下载失败，HTTP {status}")

    total = int((getattr(resp, "headers", {}) or {}).get("Content-Length") or 0)
    done = 0
    with open(part, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)

    if total and done != total:
        part.unlink(missing_ok=True)
        raise UpdateError(f"下载不完整：{done}/{total} 字节")

    os.replace(part, dest)
    return dest


def verify_sha256(path: Path, expected: str) -> bool:
    got = sha256_of(path)
    return got.lower() == (expected or "").strip().lower()


# ---------------------------------------------------------------- 进程与安装


def stop_watcher(status_path: Optional[Path] = None, *, grace: float = 8.0) -> bool:
    """停掉正在跑的 watcher，返回「是否本来在跑」。

    更新时监控进程占着 runtime/ 里的 dll，不先停会导致安装器替换文件失败。
    """
    path = Path(status_path) if status_path else paths.STATE_DIR / "status.json"
    pid: Optional[int] = None
    try:
        pid = int(json.loads(path.read_text(encoding="utf-8")).get("pid") or 0) or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not pid or not _pid_alive(pid):
        return False

    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=grace + 5,
                           creationflags=procutil.no_window_flags())
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
            for _ in range(int(grace * 10)):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
    except (OSError, subprocess.SubprocessError):
        pass

    for _ in range(int(grace * 10)):
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return True


def _watcher_pid(status_path: Optional[Path] = None) -> int:
    path = Path(status_path) if status_path else paths.STATE_DIR / "status.json"
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _roomwatch_processes(root: Path) -> list[dict]:
    """当前所有进程的 pid / exe / cmdline（取不到的字段给空值）。"""
    try:
        import psutil
    except Exception:  # noqa: BLE001 - 没有 psutil 时退回「只停 watcher」
        return []
    out: list[dict] = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            out.append(proc.info)
        except Exception:  # noqa: BLE001 - 进程在枚举中途退出属正常
            continue
    return out


def select_targets(processes: list[dict], root: Path,
                   exclude: Optional[list[int]] = None) -> list[int]:
    """挑出「跑在这个安装目录下的本程序进程」。

    两个条件缺一不可：
      1. 可执行文件在安装目录下（不能误杀开发目录或别的 python）；
      2. 命令行里出现 roomwatch（`tools\ffmpeg.exe` 虽然同目录，但它是
         watcher 的子进程，随父进程一起退，不该单点）。
    路径比较不分大小写：Windows 的路径本来就不分。
    """
    root_text = str(root).replace("\\", "/").rstrip("/").lower()
    skip = {int(pid) for pid in (exclude or [])}
    found: list[int] = []
    for item in processes or []:
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in skip:
            continue
        exe = str(item.get("exe") or "").replace("\\", "/").lower()
        cmdline = " ".join(str(part) for part in (item.get("cmdline") or [])).lower()
        if exe.startswith(root_text + "/") and "roomwatch" in cmdline:
            found.append(pid)
    return found


def _terminate_process(pid: int, *, timeout: float = 6.0) -> bool:
    """结束一个进程并等它消失。相册/守护没有要收尾的写入，可以直接终止。"""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return procutil.kill_pid(pid, timeout=timeout)
    try:
        proc = psutil.Process(pid)
    except Exception:  # noqa: BLE001
        return True
    for action in (proc.terminate, proc.kill):
        try:
            action()
            proc.wait(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - 换下一个更狠的手段
            continue
    return not procutil.pid_alive(pid)


def stop_all(
    root: Optional[Path] = None,
    *,
    grace: float = control.DEFAULT_GRACE,
    watcher: Optional[int] = None,
    processes: Optional[Callable[[Path], list[dict]]] = None,
    terminate: Optional[Callable[[int], bool]] = None,
    exclude: Optional[list[int]] = None,
) -> dict:
    """停掉所有占着安装目录的本程序进程（监控 / 相册 / 守护）。

    为什么不能只停 watcher
    ----------------------
    Inno 装之前用 RestartManager 扫「谁占着我要替换的文件」。相册
    （`python -m roomwatch gallery`）跑在包内 `runtime\\python` 上，正好占着
    `python313.dll`；它是个没有消息循环的控制台进程，RestartManager 关不掉，
    30 秒后按 Abort → 安装器返回 5 并整段回滚。**2026-10-19 真机就是这样：
    0.1.9 装了两遍都失败，版本号原地不动，随后自动重开界面还撞上
    解包被打断，弹了 "Failed to load Python DLL"。**

    返回 {"watcher": 方法名, "stopped": [pid…], "failed": [pid…]}。
    watcher 走优雅停止（等它把 mp4 收尾）；其余进程直接终止。
    """
    root = Path(root or paths.PROJECT_ROOT)
    processes = processes or _roomwatch_processes
    terminate = terminate or _terminate_process
    result: dict = {"watcher": "", "stopped": [], "failed": []}

    pid = _watcher_pid() if watcher is None else int(watcher)
    if pid and procutil.pid_alive(pid):
        outcome = control.stop_watcher(root, pid, grace=grace, force=True)
        result["watcher"] = str(outcome.get("method") or "")
        if result["watcher"] in ("graceful", "forced"):
            result["stopped"].append(pid)

    skip = [os.getpid(), *(exclude or [])]
    for target in select_targets(processes(root), root, exclude=skip):
        if target in result["stopped"]:
            continue
        if terminate(target):
            result["stopped"].append(target)
        else:
            result["failed"].append(target)
    return result


def wait_pid_exit(pid: int, *, timeout: float = 30.0) -> bool:
    """等某个进程退出，返回「是否已退出」。

    界面里点「立即更新」时必须用：界面进程自己占着 `room-watch.exe`，
    安装器替换不了正在运行的可执行文件，于是界面向更新子进程传自己的 PID，
    子进程先等它退出再动手。
    """
    if pid <= 0:
        return True
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, timeout=8,
                               creationflags=procutil.no_window_flags())
            # 不 text=True：中文 Windows 的 tasklist 输出是 GBK，
            # 按 UTF-8 解码会在 subprocess 的读线程里抛 UnicodeDecodeError。
            return str(pid).encode("ascii") in (r.stdout or b"")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run_installer(path: Path, *, timeout: float = 600.0, quiet: bool = True) -> int:
    """同步跑安装器。**只适合从安装目录之外手工调用**。

    客户更新走 :func:`handoff_install`：更新器本身是用包内 `runtime\\python` 跑的，
    见那里的说明。
    """
    if os.name != "nt":
        raise UpdateError("自动更新只支持 Windows 安装包；Linux/源码部署请用 git pull")
    args = [str(path)]
    if quiet:
        args += ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LANG=chinesesimp"]
    try:
        proc = subprocess.run(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"安装器超过 {timeout:.0f} 秒未结束，已放弃") from exc
    except OSError as exc:
        raise UpdateError(f"无法启动安装器：{exc}") from exc
    return int(proc.returncode)


def _helper_script(mode: str, delay_seconds: int, pid: int = 0) -> str:
    """生成交接脚本（**纯 ASCII**：cmd 在 936 代码页下读中文会乱）。

    路径一律从参数 `%~1` / `%~2` 取，不写进脚本文本 —— 客户机上的用户名可能是
    中文（`C:\\Users\\张三\\AppData\\...`），写死就没法保证 ASCII 了；
    而命令行参数走的是宽字符，cmd 内部按 Unicode 处理，不会乱码。

    `pid` 是更新器自己的进程号：安装器启动前必须等它退出，否则 RestartManager
    关不掉父进程，静默模式下 30 秒后按 Abort（返回 5）并回滚。

    `mode` 决定装完之后拉什么起来：
      - ``gui``      重新打开界面（客户从界面里点的更新）
      - ``watcher``  只把监控进程拉回来（原来在跑，命令行更新）
      - ``none``     什么都不拉
    安装失败时统一只做一件事：把界面打开，别让客户对着空桌面。
    """
    lines = [
        "@echo off",
        "rem generated by roomwatch.updater -- ASCII only, do not edit",
        "setlocal",
        'set "APP=%~1"',
        'set "SETUP=%~2"',
        'set "LOG=%APP%\\var\\logs\\update.log"',
        'if not exist "%APP%\\var\\logs" mkdir "%APP%\\var\\logs" >nul 2>&1',
        'if not exist "%APP%\\var\\state" mkdir "%APP%\\var\\state" >nul 2>&1',
        f"ping -n {max(1, int(delay_seconds))} 127.0.0.1 >nul 2>&1",
    ]
    if pid:
        lines += [
            "rem wait for the updater process to exit (RestartManager cannot close its parent)",
            'set "WAITFILE=%TEMP%\\room-watch-wait-%~3.txt"',
            "set /a WAIT=0",
            ":waitupdater",
            'if "%~3"=="" goto runsetup',
            # 不要用 tasklist | find：cmd 在无控制台的分离进程里，find 会一直等
            # 管道 EOF，安装器永远不启动（0.1.22 真机交接测试发现）。
            # 先写文件再 find，失败也只是立刻放行。
            '%SystemRoot%\\System32\\tasklist.exe /FI "PID eq %~3" /NH > "%WAITFILE%" 2>nul',
            'find "%~3" "%WAITFILE%" >nul',
            "if errorlevel 1 goto runsetup",
            "ping -n 2 127.0.0.1 >nul 2>&1",
            "set /a WAIT+=1",
            "if %WAIT% LSS 40 goto waitupdater",
            ":runsetup",
        ]
    lines += [
        '"%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LANG=chinesesimp >>"%LOG%" 2>&1',
        'set "RC=%ERRORLEVEL%"',
        '>>"%LOG%" echo [%DATE% %TIME%] installer rc=%RC%',
        'if not "%RC%"=="0" goto failed',
        # 成功路径清掉上次残留的失败标记
        'del /f /q "%APP%\\var\\state\\update_failed.txt" >nul 2>&1',
    ]
    if mode == "gui":
        lines += [
            'if not exist "%APP%\\room-watch.exe" goto done',
            'start "" "%APP%\\room-watch.exe"',
        ]
    elif mode == "watcher":
        lines += [
            'if not exist "%APP%\\runtime\\python\\pythonw.exe" goto done',
            'cd /d "%APP%"',
            'start "" "%APP%\\runtime\\python\\pythonw.exe" -m roomwatch start',
        ]
    lines += [
        "goto done",
        ":failed",
        '>>"%LOG%" echo installer failed, reopening gui if present',
        # 失败标记：界面下次启动时会明确告诉用户「上次更新没有完成」，
        # 而不是悄悄打开一个看起来正常的旧版本（0.1.21 真机就是这样丢了版本信息）。
        '>>"%APP%\\var\\state\\update_failed.txt" echo [%DATE% %TIME%] installer rc=%RC%',
        'if not exist "%APP%\\room-watch.exe" goto done',
        # 回滚可能还在收尾：立刻启动会撞上 PyInstaller 解包被打断
        # （弹 "Failed to load Python DLL"），等两秒再开
        "ping -n 3 127.0.0.1 >nul 2>&1",
        'start "" "%APP%\\room-watch.exe"',
        ":done",
        'del /f /q "%WAITFILE%" >nul 2>&1',
        # 安装包留在 %TEMP% 里没用；正在被安装器读时删不掉，失败也无所谓
        'del /f /q "%SETUP%" >nul 2>&1',
        "exit /b %RC%",
    ]
    return "\r\n".join(lines) + "\r\n"


def handoff_install(setup: Path, *, mode: str = "none", delay_seconds: int = 4) -> Path:
    """把安装交给一个**不属于安装目录**的进程去做，立刻返回。

    为什么不能在本进程里直接 `subprocess.run(setup.exe)`
    ------------------------------------------------
    Inno 装之前会用 RestartManager 扫「谁占着我要替换的文件」。更新器是用包内
    `runtime\\python\\python.exe` 跑的 —— 正是它占着 runtime 里的 python313.dll，
    而 RestartManager 关不掉自己的父进程。于是 30 秒后（静默模式下消息框默认
    按 Abort）安装器返回 5 并整段回滚。2026-10-19 在真实安装版上 100% 复现。
    所以：先把更新器自己结束掉，再由 `cmd.exe`（与安装目录无关）启动安装器。
    """
    if os.name != "nt":
        raise UpdateError("自动更新只支持 Windows 安装包；Linux/源码部署请用 git pull")

    setup = Path(setup)
    if not setup.is_file():
        raise UpdateError(f"安装包不存在：{setup}")

    root = Path(paths.PROJECT_ROOT)
    temp = Path(tempfile.gettempdir())
    staged = temp / setup.name
    try:
        if setup.resolve() != staged.resolve():
            shutil.copy2(setup, staged)
            # 缓存里那份已经用不上了（182MB×2 堆着），helper 用的是 %TEMP% 的暂存
            try:
                setup.unlink()
            except OSError:
                pass
    except OSError as exc:
        raise UpdateError(f"复制安装包到临时目录失败：{exc}") from exc

    helper = temp / f"room-watch-update-{os.getpid()}.cmd"
    try:
        helper.write_text(_helper_script(mode, delay_seconds, os.getpid()), encoding="ascii")
    except OSError as exc:
        raise UpdateError(f"写更新脚本失败：{exc}") from exc

    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        # 路径走参数而不是写进脚本：客户机的用户名可能是中文（见 _helper_script）
        # 第三个参数是更新器 PID：安装器启动前要等它退出。
        subprocess.Popen(["cmd.exe", "/c", str(helper), str(root), str(staged),
                          str(os.getpid())],
                         creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        raise UpdateError(f"启动更新脚本失败：{exc}") from exc
    return helper


def restart_monitor(root: Optional[Path] = None) -> bool:
    """更新完成后把监控重新拉起来（后台、无窗口）。"""
    root = Path(root or paths.PROJECT_ROOT)
    py = paths.bundled_python() or sys.executable
    exe = Path(py)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(exe), "-m", "roomwatch", "start"],
                         cwd=str(root), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 界面重启


def gui_launcher(root: Optional[Path] = None) -> Optional[list[str]]:
    """返回「打开界面」的命令行。

    分发包里界面是 PyInstaller 打出来的 `room-watch.exe`（tkinter 随包分发），
    内嵌解释器并不带 tkinter，所以不能直接 `python -m roomwatch gui`。
    """
    root = Path(root or paths.PROJECT_ROOT)
    exe = root / "room-watch.exe"
    if exe.is_file():
        return [str(exe)]
    if not (root / "config.yaml").is_file():
        return None
    return [paths.find_python(), "-m", "roomwatch", "gui"]


def relaunch_gui(root: Optional[Path] = None) -> bool:
    """更新装完后把界面重新开起来（后台、脱离当前进程）。"""
    root = Path(root or paths.PROJECT_ROOT)
    cmd = gui_launcher(root)
    if not cmd:
        return False
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(cmd, cwd=str(root), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 检查结果缓存

# 界面每次启动都去问 GitHub 太浪费（匿名 API 每小时只有 60 次），
# 所以检查结果落在 var/state/update.json，按 update.check_interval_hours 复用。
CHECK_CACHE_NAME = "update.json"


def check_cache_path(root: Optional[Path] = None) -> Path:
    root = Path(root or paths.PROJECT_ROOT)
    return root / "var" / "state" / CHECK_CACHE_NAME


def load_check_cache(root: Optional[Path] = None) -> dict:
    """读上次检查结果；读不到就返回空 dict（从不抛异常）。"""
    path = check_cache_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def failed_update_path(root: Optional[Path] = None) -> Path:
    root = Path(root or paths.PROJECT_ROOT)
    return root / "var" / "state" / "update_failed.txt"


def take_update_failure(root: Optional[Path] = None) -> str:
    """取出「上次安装失败」标记并删除；没有标记返回空串。

    交接脚本在安装器返回非 0 时写这个文件。界面启动时读一次，明确告诉用户
    「上次更新没有完成」——否则只会看到一个看起来正常的旧版本，版本号却对不上。
    """
    path = failed_update_path(root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    return text


def save_check_cache(data: dict, root: Optional[Path] = None) -> None:
    """原子写：先写 .tmp 再 replace，界面读到半个文件也不会炸。"""
    path = check_cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def check_age_hours(data: dict, *, now: Optional[datetime] = None) -> Optional[float]:
    """缓存多久以前写的（小时）；没有时间戳返回 None。"""
    from datetime import datetime as _dt

    raw = str((data or {}).get("checked_at") or "")
    if not raw:
        return None
    try:
        stamp = _dt.fromisoformat(raw)
    except ValueError:
        return None
    return max(0.0, ((now or _dt.now()) - stamp).total_seconds() / 3600.0)


def should_auto_check(data: dict, interval_hours: float) -> bool:
    """按 check_interval_hours 判断要不要再查一次；0 表示关闭后台自检。"""
    if interval_hours <= 0:
        return False
    age = check_age_hours(data)
    return age is None or age >= interval_hours


def available_version(data: dict, current: str) -> Optional[Version]:
    """缓存里的远端版本比本地新就返回它，否则 None。"""
    try:
        latest = Version.parse(str((data or {}).get("latest") or ""))
        local = Version.parse(current)
    except UpdateError:
        return None
    return latest if latest > local else None


# ---------------------------------------------------------------- 配置


def update_config(cfg: dict) -> dict:
    """把 config.yaml 的 update 段补齐默认值。"""
    raw = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    channel = str(raw.get("channel") or "stable").strip().lower()
    if channel not in ("stable", "beta"):
        channel = "stable"
    return {
        "enabled": bool(raw.get("enabled", True)),
        "repo": str(raw.get("repo") or DEFAULT_REPO),
        "channel": channel,
        "check_interval_hours": float(raw.get("check_interval_hours", 24)),
        "allow_minor": bool(raw.get("allow_minor", False)),
        "allow_downgrade": bool(raw.get("allow_downgrade", False)),
        "require_checksum": bool(raw.get("require_checksum", True)),
    }


def cache_dir() -> Path:
    return paths.CACHE_DIR / "updates"


def prune_downloads(keep: Optional[Path] = None, *, folder: Optional[Path] = None) -> int:
    """清掉缓存里除 `keep` 之外的安装包，返回删掉的个数。

    一个 setup.exe 就是 142MB。之前每更新一次就往 `var/cache/updates` 里堆一个，
    装完也不删 —— 实测两次更新后 `var/` 涨到 285MB（安装目录本身 368MB）。
    """
    base = Path(folder) if folder else cache_dir()
    if not base.is_dir():
        return 0
    keep_path: Optional[Path] = None
    if keep is not None:
        try:
            keep_path = Path(keep).resolve()
        except OSError:
            keep_path = None
    removed = 0
    for item in base.glob("room-watch-*-setup.exe*"):
        try:
            if keep_path is not None and item.resolve() == keep_path:
                continue
            item.unlink()
            removed += 1
        except OSError:
            continue
    return removed
