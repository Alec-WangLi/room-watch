"""把事件录像发到企业微信群。

为什么不能复用图片那条路
------------------------
图片只要 base64 + md5 就能直接发（见 `notifier._wecom_bot`）。视频不行：
企业微信群机器人没有「视频消息」类型，只能发**文件**，而文件必须先调
`upload_media` 换一个 `media_id`，再发 `file` 消息。

由此带来两个必须处理的现实问题：

1. **体积** —— 官方硬限制 5B~20MB，而本机一段 90 秒录像 37~99MB，
   必须转码瘦身。转码用 ffmpeg（本机 9.0，与音轨合并同一个依赖）。
2. **耗时** —— 转码 + 上传要几十秒，绝不能卡住检测主循环。
   所有重活在后台单线程里串行做，队列满了就丢弃（宁可少发一条，也不拖慢监控）。

隐私边界：视频只上传给「你自己一人的内部群」，不经过任何公网图床。
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from .config import load_config
from .paths import resolve

log = logging.getLogger("room-watch")

WECOM_UPLOAD_API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
UPLOAD_TIMEOUT = 90
SEND_TIMEOUT = 10
TRANSCODE_TIMEOUT = 300

# 体积超标时逐级提高压缩率；四级都压不下去就放弃，只发文字
CRF_LADDER = (28, 32, 36, 40)
MAX_UPLOAD_MB = 20  # 官方硬限制，配置里的 max_mb 再大也会被服务端拒


def webhook_key(webhook: str) -> Optional[str]:
    """从 send 型 webhook 里取出 key，用于拼 upload_media 地址。"""
    if not webhook:
        return None
    key = (parse_qs(urlparse(webhook).query).get("key") or [""])[0].strip()
    return key or None


def build_transcode_cmd(
    ffmpeg: str, src: str, dst: str, width: int = 854, fps: int = 12, crf: int = 30, audio: bool = True
) -> list:
    """拼 ffmpeg 转码命令。抽成纯函数，便于单测参数顺序与取值。"""
    cmd = [
        ffmpeg, "-y", "-i", src,
        "-vf", f"fps={int(fps)},scale={int(width)}:-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        # yuv420p + faststart：手机上才能边下边播，否则要等整个文件下载完
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    cmd += ["-c:a", "aac", "-b:a", "64k", "-ac", "1"] if audio else ["-an"]
    cmd.append(dst)
    return cmd


def _run(cmd: list, timeout: float) -> tuple:
    """跑一条命令。返回 (returncode, stderr尾部)。异常也算失败。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd, timeout=timeout, capture_output=True, creationflags=flags
        )
    except Exception as exc:  # noqa: BLE001 - ffmpeg 缺失/被杀都不能拖垮调用方
        return -1, str(exc)
    tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-2:]
    return proc.returncode, " / ".join(tail)


def transcode_under(
    src: str,
    dst: str,
    ffmpeg: str,
    max_bytes: int,
    width: int = 854,
    fps: int = 12,
    audio: bool = True,
    timeout: float = TRANSCODE_TIMEOUT,
) -> Optional[str]:
    """转码到 max_bytes 以内。成功返回 dst，失败返回 None。"""
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass

    last_size = 0
    for crf in CRF_LADDER:
        code, err = _run(build_transcode_cmd(ffmpeg, src, dst, width, fps, crf, audio), timeout)
        if code != 0:
            log.warning("录像转码失败（ffmpeg 返回 %s）：%s", code, err[:300])
            return None
        if not os.path.exists(dst):
            log.warning("录像转码没有产出文件")
            return None
        last_size = os.path.getsize(dst)
        if 0 < last_size <= max_bytes:
            return dst

    log.warning("录像压缩到 crf=%d 仍有 %.1f MB，超过上限 %.1f MB，放弃发送",
                CRF_LADDER[-1], last_size / 1048576, max_bytes / 1048576)
    try:
        os.remove(dst)
    except OSError:
        pass
    return None


def upload_media(webhook: str, path: str, timeout: float = UPLOAD_TIMEOUT) -> Optional[str]:
    """上传临时素材换 media_id。失败返回 None。"""
    key = webhook_key(webhook)
    if not key:
        log.warning("webhook 里没有 key，无法上传视频")
        return None

    with open(path, "rb") as f:
        r = requests.post(
            WECOM_UPLOAD_API,
            params={"key": key, "type": "file"},
            files={"media": (os.path.basename(path), f, "application/octet-stream")},
            timeout=timeout,
        )
    if r.status_code != 200:
        log.error("上传录像失败 HTTP %s: %s", r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
    except ValueError:
        log.error("上传录像返回的不是 JSON: %s", r.text[:200])
        return None
    if data.get("errcode") != 0:
        log.error("上传录像被拒 errcode=%s: %s", data.get("errcode"), data.get("errmsg"))
        return None
    return data.get("media_id")


def send_file_message(webhook: str, media_id: str, timeout: float = SEND_TIMEOUT) -> bool:
    r = requests.post(
        webhook,
        json={"msgtype": "file", "file": {"media_id": media_id}},
        timeout=timeout,
    )
    ok = r.status_code == 200 and r.json().get("errcode") == 0
    if not ok:
        log.error("发送录像消息失败: %s %s", r.status_code, r.text[:200])
    return bool(ok)


def send_text_message(webhook: str, text: str, timeout: float = SEND_TIMEOUT) -> bool:
    r = requests.post(
        webhook, json={"msgtype": "text", "text": {"content": text}}, timeout=timeout
    )
    ok = r.status_code == 200 and r.json().get("errcode") == 0
    if not ok:
        log.error("发送文字消息失败: %s %s", r.status_code, r.text[:200])
    return bool(ok)


class VideoPusher:
    """视频推送：对外只暴露 submit()，真正的活在后台线程里串行跑。"""

    def __init__(self, cfg: dict, cache_dir: Optional[str] = None):
        cfg = cfg or {}
        bot = cfg.get("wecom_bot") or {}
        video = bot.get("video") or {}

        self.channel = (cfg.get("channel") or "none").lower()
        self.webhook = (bot.get("webhook") or "").strip()
        self.send_video = bool(bot.get("send_video", True))
        self.max_bytes = int(float(video.get("max_mb", 18)) * 1024 * 1024)
        self.width = int(video.get("width", 854))
        self.fps = int(video.get("fps", 12))
        self.audio = bool(video.get("audio", True))

        self.cache_dir = cache_dir or os.path.join("var", "cache", "push")
        self._queue: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        """只有「渠道选了企业微信机器人 + webhook 已填真值 + 开关打开」才发。"""
        return (
            self.channel == "wecom_bot"
            and self.send_video
            and bool(self.webhook)
            and "REPLACE" not in self.webhook
        )

    # ---------- 对外 ----------

    def submit(self, video_path: str, caption: str = "") -> bool:
        """把一段录像丢进后台队列。队列满就丢弃，绝不阻塞。"""
        if not self.enabled or not video_path:
            return False
        if self._queue is None:
            self._queue = queue.Queue(maxsize=8)
        try:
            self._queue.put_nowait((video_path, caption))
        except queue.Full:
            log.warning("视频推送队列已满，本次录像不推送: %s", video_path)
            return False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._drain, daemon=True, name="video-push")
            self._thread.start()
        return True

    # ---------- 实际干活 ----------

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self.send(*item)
            except Exception as exc:  # noqa: BLE001 - 后台线程绝不能因为一条失败就死掉
                log.error("视频推送异常: %s", exc)

    def send(self, video_path: str, caption: str = "") -> bool:
        """同步发一段录像：转码 → 上传 → 发消息。给单测和命令行用。"""
        if not video_path or not os.path.exists(video_path):
            log.warning("录像不存在，跳过推送: %s", video_path)
            return False

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log.warning("没有 ffmpeg，录像无法压缩，跳过推送: %s", video_path)
            return False

        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError as exc:
            log.warning("建缓存目录失败（%s），跳过推送", exc)
            return False

        name = os.path.splitext(os.path.basename(video_path))[0]
        small = os.path.join(self.cache_dir, f"{name}-wx.mp4")

        cap = min(self.max_bytes, MAX_UPLOAD_MB * 1024 * 1024)
        raw_mb = os.path.getsize(video_path) / 1048576
        out = transcode_under(
            video_path, small, ffmpeg, cap, self.width, self.fps, self.audio
        )
        if not out:
            log.info("录像 %.1f MB 未能压缩到 %.1f MB 以内，只在微信里留文字说明",
                     raw_mb, cap / 1048576)
            if caption:
                send_text_message(
                    self.webhook,
                    f"{caption}\n录像 {raw_mb:.1f} MB，压缩后仍超限，仅本机可看：{video_path}",
                )
            return False

        small_mb = os.path.getsize(out) / 1048576
        media_id = upload_media(self.webhook, out)
        ok = False
        if media_id:
            if caption:
                send_text_message(self.webhook, caption)
            ok = send_file_message(self.webhook, media_id)

        try:
            os.remove(out)
        except OSError:
            pass

        if ok:
            log.info("录像已推送 %.1f MB → %.1f MB: %s", raw_mb, small_mb, video_path)
        return bool(ok)


def main(argv: Optional[list] = None) -> int:
    """命令行入口：rw push-video 录像.mp4 —— 用来单独验证视频推送。"""
    ap = argparse.ArgumentParser(description="把一段录像压缩后发到企业微信群")
    ap.add_argument("--video", required=True, help="mp4 路径")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--caption", default="", help="随视频一起发的说明文字")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = (load_config(args.config) or {}).get("notify") or {}

    pusher = VideoPusher(cfg, resolve("var/cache/push"))
    if not pusher.enabled:
        print(
            "视频推送未启用。需要：notify.channel = wecom_bot、"
            "notify.wecom_bot.webhook 填真实地址、send_video = true"
        )
        return 2

    ok = pusher.send(args.video, args.caption or "room-watch 录像测试")
    print("✓ 已发送，去企业微信里看看" if ok else "✗ 发送失败，看上面的日志")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
