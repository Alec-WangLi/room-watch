"""通知渠道扇出：一个事件 → 多条 outbox 项。

渠道能力不同，所以不是「一条消息发给所有渠道」：
- 文字：所有渠道都支持；
- 图片：企业微信机器人（base64）和通用 webhook（base64 内联）支持；
- 视频：只有企业微信机器人支持（upload_media + file 消息）。

这样排也避免了重复：图片项只发图片，文字项只发文字；重试某一项不会把
另一项再发一遍。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .notifier import Notifier
from .outbox import DeliveryResult, OutboxItem

KNOWN_CHANNELS = ("wecom_bot", "wxpusher", "serverchan", "webhook")


def channel_list(raw) -> list[str]:
    """把配置里的渠道写法统一成去重后的列表。

    支持 YAML 列表和逗号/分号分隔的字符串（设置界面存的是字符串）。
    """
    if isinstance(raw, str):
        items = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    result: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name in KNOWN_CHANNELS and name not in result:
            result.append(name)
    return result


def enabled_channels(notify_cfg: dict) -> list[str]:
    """生效的渠道列表。

    `notify.channels` 优先；没有这个键时回退旧的 `notify.channel`。
    显式写了 `channels: []` 或只含非法值，表示「一个都不启用」。
    """
    cfg = notify_cfg or {}
    raw = cfg.get("channels")
    if isinstance(raw, str) and raw.strip():
        return channel_list(raw)
    if isinstance(raw, (list, tuple)):
        return channel_list(raw)
    legacy = str(cfg.get("channel") or "none").strip().lower()
    return [legacy] if legacy in KNOWN_CHANNELS else []


def supports_image(channel: str) -> bool:
    return channel in ("wecom_bot", "webhook")


def supports_video(channel: str) -> bool:
    return channel == "wecom_bot"


def build_items(
    notify_cfg: dict,
    *,
    event_id: str,
    text: str,
    image_path: Optional[str] = None,
    video_path: Optional[str] = None,
    severity: str = "event",
    owner: str = "",
) -> list[dict]:
    """生成要入队的 outbox 项（还没有 id）。"""
    cfg = notify_cfg or {}
    bot = cfg.get("wecom_bot") or {}
    send_video = bool(bot.get("send_video", True))
    items: list[dict] = []

    for channel in enabled_channels(cfg):
        base = {
            "event_id": str(event_id or ""),
            "channel": channel,
            "severity": severity,
        }
        items.append({
            **base,
            "kind": "text",
            "payload": {"text": str(text or ""), "owner": owner,
                        "event_ts": str(event_id or "")},
            "dedupe_key": f"{event_id}:{channel}:text",
        })
        if image_path and supports_image(channel):
            items.append({
                **base,
                "kind": "image",
                "payload": {"image_path": str(image_path), "owner": owner,
                            "event_ts": str(event_id or "")},
                "dedupe_key": f"{event_id}:{channel}:image",
            })
        if video_path and supports_video(channel) and send_video:
            items.append({
                **base,
                "kind": "video",
                "payload": {"video_path": str(video_path), "owner": owner,
                            "event_ts": str(event_id or ""),
                            "text": str(text or "")},
                "dedupe_key": f"{event_id}:{channel}:video",
            })
    return items


class ChannelDispatcher:
    """把 outbox 项交给 Notifier 的具体渠道实现。"""

    def __init__(self, notify_cfg: dict, outbox_dir: str | Path):
        self.notify_cfg = notify_cfg or {}
        self.outbox_dir = str(outbox_dir)
        self.notifier = Notifier(self.notify_cfg)

    def deliver(self, item: OutboxItem) -> DeliveryResult:
        payload = dict(item.payload or {})
        payload["_item_id"] = item.id
        payload.setdefault("severity", item.severity)
        return self.notifier.deliver(
            item.channel, item.kind, payload, outbox_dir=self.outbox_dir
        )


__all__ = [
    "KNOWN_CHANNELS", "channel_list", "enabled_channels", "supports_image",
    "supports_video", "build_items", "ChannelDispatcher",
]
