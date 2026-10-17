"""推送层：把「房间有人进入」这件事发到手机。

关于渠道选择，有个必须说清楚的点：
WxPusher / Server酱 这类微信推送只能带文字或 Markdown。想让照片出现在
微信里，就必须先把照片传到一个公网可访问的地址——也就是把你的房间照片
放到公共图床上。这与「监控留证、数据不外泄」是冲突的。

因此默认设计是：微信只收文字告警，照片留在本机，用 gallery.py 起的本地
相册随时翻看。如果你确实要照片直达微信，把 channel 切成 wecom_bot：
企业微信群机器人支持 base64 直传图片，不需要任何公网 URL。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

import requests

log = logging.getLogger("room-watch")

WX_PUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
SERVER_CHAN_API = "https://sctapi.ftqq.com/{key}.send"
TIMEOUT = 10


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.channel = (self.cfg.get("channel") or "none").lower()
        self.title = self.cfg.get("title", "房间有人进入")

    def send(self, text: str, image_bytes: Optional[bytes] = None) -> bool:
        """发一条通知。返回是否成功。失败只记日志，不影响监控继续跑。"""
        try:
            if self.channel == "wxpusher":
                return self._wxpusher(text)
            if self.channel == "serverchan":
                return self._serverchan(text)
            if self.channel == "wecom_bot":
                return self._wecom_bot(text, image_bytes)
            if self.channel in ("none", "", "log"):
                log.info("[通知-仅日志] %s", text)
                return True
            log.warning("未知的通知渠道: %s，已跳过", self.channel)
            return False
        except Exception as exc:  # 网络问题绝不能拖垮监控主循环
            log.error("通知发送失败 (%s): %s", self.channel, exc)
            return False

    def _wxpusher(self, text: str) -> bool:
        c = self.cfg.get("wxpusher") or {}
        token = c.get("app_token", "").strip()
        uid = c.get("uid", "").strip()
        if not token or not uid or token.startswith("AT_xxx"):
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
        if not key or key.startswith("SCT"):
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
        if image_bytes:
            import base64

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
