"""本地抓拍相册。只用标准库，不装任何额外依赖。

    rw gallery                     # 本机访问 http://127.0.0.1:8765
    rw gallery --host 0.0.0.0      # 局域网/手机访问，必须配 token

安全边界
--------
默认只绑 127.0.0.1。一旦绑到 0.0.0.0 就等于把房间画面对整个局域网开放，
所以**没有 token 时直接拒绝启动**，不给「先开起来再说」的机会。
认证用 HTTP Basic：手机浏览器输一次就记住了，不依赖 cookie 和 JS。

注意 Basic 走明文 HTTP 只能挡「随手点开」，同一网络里抓包的人拦不住。
要放到公网，前面必须套 HTTPS（反向代理或 Tailscale）。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from .config import load_config
from .paths import CAPTURES_DIR, STATE_DIR, resolve
from .rules import gap_seconds
from . import health
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


LOOPBACK = ("127.0.0.1", "localhost", "::1")

# 认证失败时故意拖慢一点，抬高暴力猜 token 的成本。
AUTH_FAIL_DELAY = 0.4

# 客户端主动断开连接时 socket 写入抛出的异常集合。
#
# 为什么必须把 ConnectionAbortedError 也列进来：浏览器关闭页面、拖视频进度条
# 重新发 Range 请求时，会直接掐掉上一条连接。Windows 上这种半途中断报的是
# WinError 10053（ConnectionAbortedError），和 Linux 常见的
# ECONNRESET（ConnectionResetError）/ EPIPE（BrokenPipeError）不是同一个类。
# 只捕后两个的话，每拖一次进度条 socketserver 就会往 stderr 打一整段堆栈，
# 实测 2 小时 23 次、日志里全是噪音，真正的故障反而被埋掉。
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


def parse_basic_auth(header: str):
    """'Basic dXNlcjpwYXNz' → (user, password)；格式不对返回 None。"""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        raw = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, sep, password = raw.partition(":")
    if not sep:
        return None
    return user, password


def authorized(header: str, token: str) -> bool:
    """token 为空表示不启用认证（调用方必须保证只绑回环地址）。

    用户名和密码字段都接受 token：手机端密码管理器经常把凭据填到用户名里，
    为了这个把用户挡在门外没有意义。
    """
    if not token:
        return True
    creds = parse_basic_auth(header)
    if creds is None:
        return False
    user, password = creds
    return hmac.compare_digest(password, token) or hmac.compare_digest(user, token)


def check_bind(host: str, token: str) -> str | None:
    """返回错误说明；None 表示可以启动。"""
    if host in LOOPBACK:
        return None
    if not token:
        return (
            f"拒绝启动：--host {host} 会把房间画面暴露到局域网，但没有配置 token。\n"
            "请在 config.yaml（或 config.local.yaml）的 gallery.token 里设一个长密码，"
            "或加 --token 参数。"
        )
    if len(token) < 12:
        return f"拒绝启动：token 只有 {len(token)} 位，太短了，请至少 12 位。"
    return None


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>房间抓拍</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;margin:0;padding:20px;background:#f6f6f4;color:#222}
h1{font-size:17px;font-weight:500;margin:0 0 4px}
p.meta{color:#777;font-size:13px;margin:0 0 6px}
.on,.off{font-weight:500;padding:1px 7px;border-radius:20px;font-size:12px}
.on{background:#E1F5EE;color:#0F6E56}
.off{background:#FAECE7;color:#993C1D}
.day{margin-bottom:26px}
.day h2{font-size:14px;font-weight:500;color:#444;margin:0 0 10px;padding-bottom:6px;border-bottom:1px solid #e2e2de}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
figure{margin:0;background:#fff;border:1px solid #e6e6e2;border-radius:10px;overflow:hidden}
figure img,video{width:100%;display:block;background:#000}
figcaption{font-size:12px;color:#666;padding:6px 9px;display:flex;justify-content:space-between;gap:8px}
.has-video figcaption{color:#0F6E56}
.badge{font-size:11px;background:#E1F5EE;color:#0F6E56;padding:1px 6px;border-radius:10px;white-space:nowrap}
.empty{color:#888;font-size:14px}
</style></head><body>
<h1>房间抓拍</h1>
<p class="meta">{status}</p>
<p class="meta">共 {count} 条 · 生成于 {now}</p>
{days}
<script>
document.addEventListener('click',function(e){{
  if(e.target.tagName==='IMG'){{window.open(e.target.src,'_blank');}}
}});
</script>
</body></html>
"""

DAY_HTML = """<div class="day"><h2>{day}</h2><div class="grid">{items}</div></div>"""

ITEM_HTML = (
    '<figure><img src="{src}" loading="lazy">'
    "<figcaption><span>{time}</span></figcaption></figure>"
)

# 有录像时，把抓拍图当作视频封面：扫一眼就知道是谁，想看过程再点开。
# preload="metadata" 是为了不一次性把几十 MB 视频拉下来；
# 拖动进度条能否工作，取决于服务端支不支持 Range——见 serve_media。
VIDEO_ITEM_HTML = (
    '<figure class="has-video">'
    '<video src="{video}" poster="{src}" controls preload="metadata" playsinline></video>'
    '<figcaption><span>{time}</span><span class="badge">有录像</span></figcaption>'
    "</figure>"
)

VIDEO_ONLY_HTML = (
    '<figure class="has-video">'
    '<video src="{video}" controls preload="metadata" playsinline></video>'
    '<figcaption><span>{time}</span><span class="badge">有录像</span></figcaption>'
    "</figure>"
)


def collect(captures_dir: str):
    """返回 [(日期, [(图片, 时间, 录像)])]，按日期倒序。

    图片与录像靠文件名 stem 配对：090310.jpg 与 090310.mp4 是一条事件的两面。
    两者都可能缺——录像开了但人走得太快没出图，或误检出了图却没录上。
    """
    root = os.path.abspath(captures_dir)
    if not os.path.isdir(root):
        return []

    days = []
    for entry in sorted(os.listdir(root), reverse=True):
        day_path = os.path.join(root, entry)
        if not os.path.isdir(day_path):
            continue

        pairs: dict = {}
        for name in os.listdir(day_path):
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in (".jpg", ".jpeg", ".mp4"):
                continue
            slot = pairs.setdefault(stem, {"img": None, "video": None})
            if ext == ".mp4":
                slot["video"] = f"captures/{entry}/{name}"
            else:
                slot["img"] = f"captures/{entry}/{name}"

        items = []
        for stem in sorted(pairs, reverse=True):
            slot = pairs[stem]
            if not slot["img"] and not slot["video"]:
                continue
            label = (
                f"{stem[0:2]}:{stem[2:4]}:{stem[4:6]}"
                if len(stem) >= 6 and stem.isdigit()
                else stem
            )
            items.append((slot["img"], label, slot["video"]))
        if items:
            days.append((entry, items))
    return days


def render_item(src, time_label: str, video) -> str:
    """一条事件怎么显示：有录像就给出可播的视频（抓拍图当封面）。"""
    safe_time = html.escape(time_label)
    if video:
        safe_video = html.escape(video)
        if src:
            return VIDEO_ITEM_HTML.format(
                video=safe_video, src=html.escape(src), time=safe_time
            )
        return VIDEO_ONLY_HTML.format(video=safe_video, time=safe_time)
    return ITEM_HTML.format(src=html.escape(src or ""), time=safe_time)


def read_status(status_path: str, stale_after: float = 120.0) -> str:
    """把 watcher 写下的状态变成一句话。看最后画面距今多久最直接。"""
    if not os.path.exists(status_path):
        return '<span class="off">监控未运行</span> · 没有找到 status.json'

    try:
        with open(status_path, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return '<span class="off">状态文件读不了</span>'

    # 同样不能信文件里的 stale：进程被强杀时它永远停在 false。
    # 用「最后画面距现在多久」在读取这一刻重算。
    gap = gap_seconds(s.get("last_frame_at"))
    stale = gap is None or gap > stale_after
    badge = '<span class="off">已停止</span>' if stale else '<span class="on">在线</span>'
    ago = f"{int(gap)} 秒前" if gap is not None else "未知"
    parts = [
        badge,
        f"已运行 {html.escape(str(s.get('uptime', '?')))}",
        f"最后画面 {ago}",
        f"今日抓拍 {s.get('events_today', 0)} 次",
    ]
    if not s.get("camera_ok", True):
        parts.append('<span class="off">摄像头异常</span>')
    if s.get("dry_run"):
        parts.append("dry-run 未推送")
    return " · ".join(parts)


def render(
    captures_dir: str, status_path: str = "status.json", stale_after: float = 120.0
) -> str:
    days = collect(captures_dir)
    if not days:
        body = '<p class="empty">还没有抓拍。先跑 <code>rw dry-run</code> 看看。</p>'
        count = 0
    else:
        count = sum(len(i) for _d, i in days)
        body = "".join(
            DAY_HTML.format(
                day=html.escape(day),
                items="".join(render_item(src, t, video) for src, t, video in items),
            )
            for day, items in days
        )
    # 用 replace 而不是 str.format：CSS 里的花括号会被 format 当成占位符
    return (
        HTML.replace("{count}", str(count))
        .replace("{now}", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("{days}", body)
        .replace("{status}", read_status(status_path, stale_after))
    )


class Handler(SimpleHTTPRequestHandler):
    captures_dir: str = "captures"
    status_file: str = "status.json"
    stale_after: float = 120.0
    auth_token: str = ""

    def _auth_ok(self) -> bool:
        if authorized(self.headers.get("Authorization", ""), self.auth_token):
            return True
        time.sleep(AUTH_FAIL_DELAY)
        body = "需要登录：用户名随意，密码填 gallery.token".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="room-watch", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def do_GET(self):
        if not self._auth_ok():
            return
        if self.path in ("/", "/index.html"):
            page = render(self.captures_dir, self.status_file, self.stale_after).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path in ("/status", "/status.json"):
            self._serve_status()
            return
        if self.path.startswith("/captures/"):
            return self.serve_media()
        self.send_error(404)

    def do_HEAD(self):
        """浏览器探测视频时长时会先发 HEAD。"""
        if not self._auth_ok():
            return
        return self.serve_media()

    def _resolve_media_path(self):
        """URL 路径 -> 本地绝对路径。同时负责挡住 ../ 目录穿越。

        以 captures_dir（而不是「工程目录下的 captures」）为根——
        否则 `gallery.py --captures D:\\elsewhere` 会因为找不到文件全部 404。
        """
        clean = unquote(urlparse(self.path).path)
        rel = clean.lstrip("/")
        if not rel.startswith("captures/"):
            return None
        sub = rel[len("captures/"):].strip()
        if not sub:
            return None
        root = os.path.normpath(os.path.abspath(self.captures_dir))
        target = os.path.normpath(os.path.join(root, sub.replace("/", os.sep)))
        # 任何跳出 captures 的企图一律驳回
        if target != root and not target.startswith(root + os.sep):
            return None
        return target

    def serve_media(self) -> None:
        """回图片或视频。

        为什么不直接 super().do_GET()：标准库的 SimpleHTTPRequestHandler
        不支持 Range 请求，视频只能整段下载、且拖不动进度条。
        几分钟的录像不能快进、不能跳到最后看结尾，基本就废了一半。
        所以这里自己解析 Range 并回 206。
        """
        path = self._resolve_media_path()
        if path is None or not os.path.isfile(path):
            self.send_error(404)
            return

        file_size = os.path.getsize(path)
        start, end = 0, file_size - 1
        status = 200

        rng = self.headers.get("Range")
        if rng:
            matched = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if matched:
                raw_first, raw_last = matched.group(1), matched.group(2)
                if raw_first:
                    start = int(raw_first)
                    end = int(raw_last) if raw_last else file_size - 1
                elif raw_last:
                    start = max(0, file_size - int(raw_last))  # bytes=-500 = 末尾 500 字节
                    end = file_size - 1
                end = min(end, file_size - 1)
                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if self.command == "HEAD":
            return

        try:
            f = open(path, "rb")
        except OSError as exc:
            # 文件在列目录后被删/被占用——回 500，不要把半截响应留在连接里
            self.send_error(500, f"打不开文件: {exc}")
            return

        with f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except CLIENT_GONE:
                # 拖进度条/关页面时浏览器会主动掐掉上一个请求，这不是错误
                pass

    def _serve_status(self):
        if not os.path.exists(self.status_file):
            body = json.dumps({"online": False, "reason": "status.json not found"}).encode()
        else:
            try:
                with open(self.status_file, "rb") as f:
                    body = f.read()
            except OSError:
                body = json.dumps({"online": False, "reason": "unreadable"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class GalleryServer(ThreadingHTTPServer):
    """把「客户端跑掉」从故障里摘出来，其余异常照旧打印堆栈。

    处理函数里的 try 只能盖住媒体正文那段写入；连接在写响应头、写首页 HTML
    或认证 401 时断开，异常会一路抛到 socketserver 的 handle_error，
    标准实现一律 print 成「Exception occurred during processing of request」。
    这类日志对排查毫无价值（对方已经走了，回什么都不可能送达），却足以把真
    故障淹掉，所以在这一层统一吞掉。
    """

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)


def write_pid_file(path: str | Path, pid: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(int(pid)), encoding="utf-8")


def remove_pid_file(path: str | Path, pid: int) -> None:
    """只删内容等于自己 PID 的文件，避免把新实例的 PID 文件删掉。"""
    target = Path(path)
    try:
        if target.read_text(encoding="utf-8").strip() == str(int(pid)):
            target.unlink()
    except (OSError, ValueError):
        pass


def open_server(host: str, port: int, handler) -> tuple:
    """绑定端口；首选端口不可用时换一个稳定端口，最后才让系统分配。

    Windows 会为 Hyper-V/WinNAT 等保留整段 TCP 端口（本机实测
    8748–8847），落在保留段里的 bind 会以 WinError 10013 失败。
    先试 +10000/+20000 这类固定偏移，能保持「每次启动地址一样」，
    方便用户收藏相册地址；都不行再让系统分配，并把实际端口写进
    状态文件供界面读取。
    """
    reason: Optional[OSError] = None
    candidates = []
    if port:
        candidates.append(int(port))
        for offset in (10000, 20000):
            candidate = int(port) + offset
            if candidate < 65536:
                candidates.append(candidate)
    else:
        candidates.append(0)

    for candidate in candidates:
        try:
            server = GalleryServer((host, candidate), handler)
            return server, int(server.server_address[1]), reason
        except OSError as exc:
            if reason is None:
                reason = exc
    server = GalleryServer((host, 0), handler)
    return server, int(server.server_address[1]), reason


def write_state_file(
    path: str | Path, *, pid: int, host: str, port: int,
    started_at: Optional[str] = None,
) -> None:
    """记录相册实际在哪个地址监听，界面据此打开正确的 URL。"""
    url = f"http://{host}:{int(port)}/"
    health.write_json_atomic(Path(path), {
        "pid": int(pid),
        "host": host,
        "port": int(port),
        "url": url,
        "started_at": started_at or datetime.now().isoformat(timespec="seconds"),
    })


def remove_state_file(path: str | Path, pid: int) -> None:
    """只删自己写的状态文件，避免把新实例的地址删掉。"""
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) == int(pid):
            target.unlink()
    except (OSError, ValueError, TypeError):
        pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="本地抓拍相册")
    ap.add_argument("--config", default=str(resolve("config.yaml")),
                    help="从这份配置的 gallery 段读 host/port/token")
    ap.add_argument("--captures", default=str(CAPTURES_DIR))
    ap.add_argument("--host", default=None, help="默认读配置，未配置则 127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--token", default=None, help="访问密码；绑非回环地址时必填")
    ap.add_argument("--status-file", default=str(STATE_DIR / "status.json"))
    ap.add_argument("--stale-after", type=float, default=120.0)
    ap.add_argument("--pid-file", default=str(STATE_DIR / "gallery.pid"),
                    help="运行中写 PID，供守护进程判活")
    ap.add_argument("--state-file", default=None,
                    help="实际监听地址（默认与 pid 文件同目录的 gallery.json）")
    args = ap.parse_args(argv)

    gallery_cfg: dict = {}
    if os.path.exists(args.config):
        try:
            gallery_cfg = (load_config(args.config) or {}).get("gallery") or {}
        except Exception as exc:  # noqa: BLE001
            print(f"读配置失败（{args.config}）：{exc}，改用命令行参数与默认值")

    host = args.host or str(gallery_cfg.get("host") or "127.0.0.1")
    port = args.port if args.port is not None else int(gallery_cfg.get("port") or 8765)
    token = (args.token if args.token is not None else str(gallery_cfg.get("token") or "")).strip()

    problem = check_bind(host, token)
    if problem:
        print(problem)
        raise SystemExit(2)

    Handler.captures_dir = os.path.abspath(args.captures)
    Handler.status_file = os.path.abspath(args.status_file)
    Handler.stale_after = args.stale_after
    Handler.auth_token = token

    pid_file = os.path.abspath(args.pid_file)
    state_file = os.path.abspath(
        args.state_file or str(Path(pid_file).with_name("gallery.json"))
    )
    server, actual_port, fallback_reason = open_server(host, port, Handler)
    if fallback_reason is not None:
        print(
            f"端口 {port} 不可用（{fallback_reason}），"
            f"已改用 {actual_port}（Windows 保留端口段常见原因）"
        )
    url = f"http://{host}:{actual_port}/"
    if token:
        print(f"相册地址 {url}（需要登录：用户名随意，密码 = gallery.token）")
    else:
        print(f"相册地址 {url}（仅本机，Ctrl+C 停止）")
    if host not in LOOPBACK:
        print("提示：Basic 认证走明文 HTTP，只适合家庭/办公内网；公网请套 HTTPS。")
    write_pid_file(pid_file, os.getpid())
    write_state_file(state_file, pid=os.getpid(), host=host, port=actual_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        remove_pid_file(pid_file, os.getpid())
        remove_state_file(state_file, os.getpid())


if __name__ == "__main__":
    main()
