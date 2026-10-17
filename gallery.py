"""本地抓拍相册。只用标准库，不装任何额外依赖。

    python gallery.py              # 只本机访问 http://127.0.0.1:8765
    python gallery.py --port 9000

手机上看的话需要内网穿透（frp / cpolar / Tailscale 等），
不要把 --host 暴露到公网：这是你房间的照片。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logic import gap_seconds
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
        body = '<p class="empty">还没有抓拍。先跑 <code>python watcher.py --dry-run</code> 看看。</p>'
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

    def do_GET(self):
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
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 拖进度条时浏览器会主动掐掉上一个请求，这不是错误
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


def main() -> None:
    ap = argparse.ArgumentParser(description="本地抓拍相册")
    ap.add_argument("--captures", default="captures")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--status-file", default="status.json")
    ap.add_argument("--stale-after", type=float, default=120.0)
    args = ap.parse_args()

    Handler.captures_dir = os.path.abspath(args.captures)
    Handler.status_file = os.path.abspath(args.status_file)
    Handler.stale_after = args.stale_after
    if args.host not in ("127.0.0.1", "localhost"):
        print("警告：你正在把相册暴露到局域网之外，这是你房间的照片。确认吗？")

    url = f"http://{args.host}:{args.port}/"
    print(f"相册地址 {url} （Ctrl+C 停止）")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
