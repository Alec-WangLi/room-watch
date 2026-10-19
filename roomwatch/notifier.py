"""推送层：把「房间有人进入」这件事发到手机。

关于渠道选择，有个必须说清楚的点：
WxPusher / Server酱 这类微信推送只能带文字或 Markdown。想让照片出现在
微信里，就必须先把照片传到一个公网可访问的地址——也就是把你的房间照片
放到公共图床上。这与「监控留证、数据不外泄」是冲突的。

因此默认设计是：微信只收文字告警，照片留在本机，用 gallery.py 起的本地
相册随时翻看。如果你确实要照片直达微信，把 channel 切成 wecom_bot：
企业微信群机器人支持 base64 直传图片，不需要任何公网 URL。

0.1.16 起新增 `deliver()`：它返回可重试/永久失败的分类，供 outbox worker
使用；旧的 `send()` 保留布尔返回，CLI 与旧调用方不受影响。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import requests

from .outbox import DeliveryResult

log = logging.getLogger("room-watch")

WX_PUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
SERVER_CHAN_API = "https://sctapi.ftqq.com/{key}.send"
TIMEOUT = 10
WEBHOOK_MAX_IMAGE_MB = 1


def _http_failure(r) -> Optional[DeliveryResult]:
    """非 2xx 的统一分类：限流/5xx 可重试，其余 4xx 永久失败。"""
    code = int(getattr(r, "status_code", 0) or 0)
    text = str(getattr(r, "text", "") or "")[:200]
    if code in (408, 429) or code >= 500:
        return DeliveryResult(False, retryable=True, error=f"HTTP {code}: {text}")
    if code < 200 or code >= 300:
        return DeliveryResult(False, retryable=False, error=f"HTTP {code}: {text}")
    return None


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.channel = (self.cfg.get("channel") or "none").lower()
        self.title = self.cfg.get("title", "房间有人进入")

    # ------------------------------------------------------------ 旧接口

    def send(self, text: str, image_bytes: Optional[bytes] = None) -> bool:
        """发一条通知。返回是否成功。失败只记日志，不影响监控继续跑。"""
        try:
            if self.channel == "wxpusher":
                return self._wxpusher(text)
            if self.channel == "serverchan":
                return self._serverchan(text)
            if self.channel == "wecom_bot":
                return self._wecom_bot(text, image_bytes)
            if self.channel == "webhook":
                return self._webhook_result({"text": text}, kind="text").ok
            if self.channel in ("none", "", "log"):
                log.info("[通知-仅日志] %s", text)
                return True
            log.warning("未知的通知渠道: %s，已跳过", self.channel)
            return False
        except Exception as exc:  # 网络问题绝不能拖垮监控主循环
            log.error("通知发送失败 (%s): %s", self.channel, exc)
            return False

    # ------------------------------------------------------------ outbox 接口

    def deliver(
        self,
        channel: str,
        kind: str,
        payload: dict,
        *,
        outbox_dir: Optional[str] = None,
    ) -> DeliveryResult:
        """outbox worker 调用的统一入口，返回可重试分类。"""
        channel = str(channel or "").lower()
        payload = payload or {}
        if kind == "text":
            return self._deliver_text(channel, payload)
        if kind == "image":
            return self._deliver_image(channel, payload)
        if kind == "video":
            return self._deliver_video(channel, payload, outbox_dir or "var/outbox")
        return DeliveryResult(False, retryable=False, error=f"未知通知类型：{kind}")

    def _deliver_text(self, channel: str, payload: dict) -> DeliveryResult:
        text = str(payload.get("text") or "")
        if channel == "wecom_bot":
            return self._wecom_text_result(text)
        if channel == "wxpusher":
            return self._wxpusher_result(text)
        if channel == "serverchan":
            return self._serverchan_result(text)
        if channel == "webhook":
            return self._webhook_result(payload, kind="text")
        return DeliveryResult(False, retryable=False, error=f"未知渠道：{channel}")

    def _deliver_image(self, channel: str, payload: dict) -> DeliveryResult:
        path = str(payload.get("image_path") or "")
        if not path or not os.path.isfile(path):
            return DeliveryResult(False, retryable=False, error=f"图片不存在：{path}")
        if channel == "wecom_bot":
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                return DeliveryResult(False, retryable=False, error=f"读图片失败：{exc}")
            return self._wecom_image_result(data)
        if channel == "webhook":
            return self._webhook_result(payload, kind="image", image_path=path)
        return DeliveryResult(False, retryable=False, error=f"渠道不支持图片：{channel}")

    # ------------------------------------------------------------ 企业微信

    def _wecom_webhook(self) -> Optional[str]:
        webhook = str((self.cfg.get("wecom_bot") or {}).get("webhook") or "").strip()
        if not webhook or "REPLACE" in webhook.upper():
            return None
        return webhook

    def _post_wecom(self, payload: dict, timeout: float = TIMEOUT) -> DeliveryResult:
        webhook = self._wecom_webhook()
        if not webhook:
            return DeliveryResult(False, retryable=False, error="企业微信 webhook 未配置")
        try:
            r = requests.post(webhook, json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络错误可重试
            return DeliveryResult(False, retryable=True, error=f"网络错误：{exc}")
        failed = _http_failure(r)
        if failed is not None:
            return failed
        try:
            data = r.json()
        except ValueError:
            return DeliveryResult(False, retryable=False, error="企业微信返回的不是 JSON")
        code = data.get("errcode")
        if code == 0:
            return DeliveryResult(True)
        if code in (-1, 45009):
            return DeliveryResult(False, retryable=True, error=f"errcode {code}")
        return DeliveryResult(
            False, retryable=False,
            error=f"errcode {code}: {data.get('errmsg', '')}",
        )

    def _wecom_text_result(self, text: str) -> DeliveryResult:
        return self._post_wecom({"msgtype": "text", "text": {"content": text}})

    def _wecom_image_result(self, image_bytes: bytes) -> DeliveryResult:
        payload = {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(image_bytes).decode(),
                "md5": hashlib.md5(image_bytes).hexdigest(),
            },
        }
        return self._post_wecom(payload)

    # ------------------------------------------------------------ WxPusher

    def _wxpusher_result(self, text: str) -> DeliveryResult:
        c = self.cfg.get("wxpusher") or {}
        token = str(c.get("app_token", "")).strip()
        uid = str(c.get("uid", "")).strip()
        if not token or not uid or "replace_me" in token.lower():
            return DeliveryResult(False, retryable=False, error="WxPusher 未配置")
        payload = {
            "appToken": token,
            "content": text,
            "summary": self.title,
            "contentType": 1,
            "uids": [uid],
            "verifyPay": False,
        }
        try:
            r = requests.post(WX_PUSHER_API, json=payload, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, retryable=True, error=f"网络错误：{exc}")
        failed = _http_failure(r)
        if failed is not None:
            return failed
        try:
            data = r.json()
        except ValueError:
            return DeliveryResult(False, retryable=False, error="WxPusher 返回的不是 JSON")
        if data.get("code") == 1000:
            return DeliveryResult(True)
        return DeliveryResult(
            False, retryable=False,
            error=f"WxPusher code={data.get('code')}: {data.get('msg', '')}",
        )

    # ------------------------------------------------------------ ServerChan

    def _serverchan_result(self, text: str) -> DeliveryResult:
        c = self.cfg.get("serverchan") or {}
        key = str(c.get("sendkey", "")).strip()
        if not key or "replace_me" in key.lower():
            return DeliveryResult(False, retryable=False, error="ServerChan 未配置")
        try:
            r = requests.post(
                SERVER_CHAN_API.format(key=key),
                data={"title": self.title, "desp": text},
                timeout=TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, retryable=True, error=f"网络错误：{exc}")
        failed = _http_failure(r)
        if failed is not None:
            return failed
        try:
            data = r.json()
        except ValueError:
            return DeliveryResult(False, retryable=False, error="ServerChan 返回的不是 JSON")
        if data.get("code") == 0:
            return DeliveryResult(True)
        return DeliveryResult(
            False, retryable=False,
            error=f"ServerChan code={data.get('code')}: {data.get('message', '')}",
        )

    # ------------------------------------------------------------ 通用 webhook

    def _webhook_result(
        self, payload: dict, *, kind: str, image_path: Optional[str] = None
    ) -> DeliveryResult:
        c = self.cfg.get("webhook") or {}
        url = str(c.get("url") or "").strip()
        if not url or "replace_me" in url.lower():
            return DeliveryResult(False, retryable=False, error="通用 webhook 未配置")
        if not url.lower().startswith(("http://", "https://")):
            return DeliveryResult(False, retryable=False, error="通用 webhook 必须是 http(s) URL")

        body = {
            "event": "room_watch",
            "kind": kind,
            "severity": str(payload.get("severity") or "event"),
            "time": str(payload.get("event_ts") or ""),
            "text": str(payload.get("text") or ""),
            "owner": str(payload.get("owner") or ""),
            "has_image": False,
            "image_base64": None,
        }
        if image_path:
            try:
                data = Path(image_path).read_bytes()
            except OSError as exc:
                return DeliveryResult(False, retryable=False, error=f"读图片失败：{exc}")
            max_mb = int(float(c.get("max_image_mb", WEBHOOK_MAX_IMAGE_MB)))
            if len(data) <= max(0, max_mb) * 1024 * 1024:
                body["has_image"] = True
                body["image_base64"] = base64.b64encode(data).decode()

        timeout = float(c.get("timeout_seconds", TIMEOUT))
        try:
            r = requests.post(url, json=body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, retryable=True, error=f"网络错误：{exc}")
        failed = _http_failure(r)
        if failed is not None:
            return failed
        return DeliveryResult(True)

    # ------------------------------------------------------------ 视频

    def _deliver_video(
        self, channel: str, payload: dict, outbox_dir: str
    ) -> DeliveryResult:
        if channel != "wecom_bot":
            return DeliveryResult(False, retryable=False, error=f"渠道不支持视频：{channel}")
        bot = self.cfg.get("wecom_bot") or {}
        if not bool(bot.get("send_video", True)):
            return DeliveryResult(False, retryable=False, error="录像推送已关闭")
        webhook = self._wecom_webhook()
        if not webhook:
            return DeliveryResult(False, retryable=False, error="企业微信 webhook 未配置")

        video_path = str(payload.get("video_path") or "")
        if not video_path or not os.path.isfile(video_path):
            return DeliveryResult(False, retryable=False, error=f"录像不存在：{video_path}")

        video_cfg = bot.get("video") or {}
        from .paths import ffmpeg_exe  # noqa: PLC0415 - 分发包自带 ffmpeg

        ffmpeg = shutil.which(str(video_cfg.get("ffmpeg") or "ffmpeg")) or ffmpeg_exe()
        if not ffmpeg:
            return DeliveryResult(False, retryable=False, error="没有 ffmpeg，无法压缩录像")

        from . import videopush  # noqa: PLC0415 - 避免与 notifier 的循环导入

        item_id = payload.get("_item_id") or "tmp"
        temp = Path(outbox_dir) / f"item-{item_id}.mp4"
        temp.parent.mkdir(parents=True, exist_ok=True)
        cap_mb = min(int(float(video_cfg.get("max_mb", 18))), videopush.MAX_UPLOAD_MB)
        cap = max(1, cap_mb) * 1024 * 1024

        try:
            reuse = temp.is_file() and 0 < temp.stat().st_size <= cap
        except OSError:
            reuse = False
        if not reuse:
            out = videopush.transcode_under(
                video_path, str(temp), ffmpeg, cap,
                int(video_cfg.get("width", 854)),
                int(video_cfg.get("fps", 12)),
                bool(video_cfg.get("audio", True)),
            )
            if not out:
                return DeliveryResult(
                    False, retryable=False,
                    error=f"录像压缩后仍超过 {cap_mb} MB",
                )

        media_id = videopush.upload_media(webhook, str(temp))
        if not media_id:
            return DeliveryResult(False, retryable=True, error="上传录像失败（网络或服务端错误）")
        ok = videopush.send_file_message(webhook, media_id)
        if not ok:
            return DeliveryResult(False, retryable=True, error="发送录像消息失败")
        try:
            temp.unlink()
        except OSError:
            pass
        return DeliveryResult(True)

    # ------------------------------------------------------------ 旧渠道实现（保留给 send / 测试）

    def _wxpusher(self, text: str) -> bool:
        c = self.cfg.get("wxpusher") or {}
        token = c.get("app_token", "").strip()
        uid = c.get("uid", "").strip()
        if not token or not uid or "replace_me" in token.lower():
            log.warning("WxPusher 未配置 app_token / uid，跳过推送")
            return False

        payload = {
            "appToken": token,
            "content": text,
            "summary": self.title,
            "contentType": 1,
            "uids": [uid],
            "verifyPay": False,
        }
        r = requests.post(WX_PUSHER_API, json=payload, timeout=TIMEOUT)
        ok = r.status_code == 200 and r.json().get("code") == 1000
        if not ok:
            log.error("WxPusher 返回异常: %s %s", r.status_code, r.text[:200])
        return bool(ok)

    def _serverchan(self, text: str) -> bool:
        c = self.cfg.get("serverchan") or {}
        key = c.get("sendkey", "").strip()
        # 只认占位符，不能按前缀判断——真实 SendKey 同样以 SCT 开头，
        # 旧写法 key.startswith("SCT") 会把真 key 当成没配置，永久静默。
        if not key or "replace_me" in key.lower():
            log.warning("Server酱 未配置 sendkey，跳过推送")
            return False

        r = requests.post(
            SERVER_CHAN_API.format(key=key),
            data={"title": self.title, "desp": text},
            timeout=TIMEOUT,
        )
        ok = r.status_code == 200 and r.json().get("code") == 0
        if not ok:
            log.error("Server酱 返回异常: %s %s", r.status_code, r.text[:200])
        return bool(ok)

    def _wecom_bot(self, text: str, image_bytes: Optional[bytes]) -> bool:
        """企业微信群机器人。唯一能干净地把照片直接发到微信的渠道。"""
        c = self.cfg.get("wecom_bot") or {}
        webhook = c.get("webhook", "").strip()
        if not webhook or "REPLACE" in webhook:
            log.warning("企业微信机器人未配置 webhook，跳过推送")
            return False

        ok = True
        if image_bytes and not isinstance(image_bytes, (bytes, bytearray)):
            # 防御：调用方把标题/正文误传进第二个参数时，别让整条推送挂掉
            log.warning("image_bytes 不是字节（%s），本次只发文字", type(image_bytes).__name__)
            image_bytes = None
        if image_bytes:
            payload = {
                "msgtype": "image",
                "image": {
                    "base64": base64.b64encode(image_bytes).decode(),
                    "md5": hashlib.md5(image_bytes).hexdigest(),
                },
            }
            r = requests.post(webhook, json=payload, timeout=TIMEOUT)
            ok = r.status_code == 200 and r.json().get("errcode") == 0
            if not ok:
                log.error("企业微信图片发送失败: %s %s", r.status_code, r.text[:200])

        r = requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=TIMEOUT,
        )
        text_ok = r.status_code == 200 and r.json().get("errcode") == 0
        if not text_ok:
            log.error("企业微信文本发送失败: %s %s", r.status_code, r.text[:200])
        return bool(ok and text_ok)
