"""通知渠道扇出：一个事件 → 多条 outbox 项。

渠道能力不同，所以不是「一条消息发给所有渠道」：
- 文字：所有渠道都支持；
- 图片：企业微信机器人（base64）和通用 webhook（base64 内联）支持；
- 视频：只有企业微信机器人支持（upload_media + file 消息）。

这样排也避免了重复：图片项只发图片，文字项只发文字；重试某一项不会把
另一项再发一遍。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .notifier import Notifier
from .outbox import DeliveryResult, OutboxItem, OutboxStore

log = logging.getLogger("room-watch")

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


class DeliveryWorker:
    """从 outbox 领取并投递；文字/图片快道与视频慢道各一个实例。

    SQLite 连接不能跨线程共享，所以 worker 在自己的线程里创建 OutboxStore；
    `flush()` 是给 `guard --once` 用的同步路径，也在调用线程里临时开连接。
    """

    def __init__(
        self,
        db_path: str | Path,
        dispatcher: ChannelDispatcher,
        *,
        kinds: tuple = ("text", "image"),
        poll_seconds: float = 2.0,
        maintenance_seconds: float = 600.0,
        lease_seconds: float = 120.0,
        media_dir: str | Path = "var/outbox",
        max_age_days: float = 7.0,
        max_total_bytes: int = 2 * 1024 ** 3,
        stop_event: Optional[threading.Event] = None,
    ):
        self.db_path = str(db_path)
        self.dispatcher = dispatcher
        self.kinds = tuple(kinds)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.maintenance_seconds = max(30.0, float(maintenance_seconds))
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.media_dir = str(media_dir)
        self.max_age_days = float(max_age_days)
        self.max_total_bytes = int(max_total_bytes)
        self._stop = stop_event or threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        name = "delivery-" + "-".join(self.kinds or ("all",))
        self._thread = threading.Thread(target=self.run, daemon=True, name=name)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, timeout))

    # ------------------------------------------------------------ 执行

    def _open(self) -> OutboxStore:
        return OutboxStore(self.db_path)

    def run_once(self, store: Optional[OutboxStore] = None) -> bool:
        """领取一条并投递。返回是否真的处理了一条。"""
        own = store is None
        store = store or self._open()
        try:
            item = store.claim_next(
                lease_seconds=self.lease_seconds, kinds=self.kinds
            )
            if item is None:
                return False
            try:
                result = self.dispatcher.deliver(item)
            except Exception as exc:  # noqa: BLE001 - 单条失败不能杀死 worker
                log.warning("投递异常（%s/%s）：%s", item.channel, item.kind, exc)
                result = DeliveryResult(False, retryable=True, error=f"投递异常：{exc}")
            store.record_channel_result(item.channel, result.ok, result.error)
            if result.ok:
                store.mark_delivered(item.id)
            else:
                status = store.mark_retry(
                    item.id, error=result.error,
                    retryable=result.retryable, jitter=0.1,
                )
                if status == "failed_permanent":
                    log.error("投递永久失败（%s/%s）：%s",
                              item.channel, item.kind, result.error)
            return True
        finally:
            if own:
                store.close()

    def flush(self, max_items: int = 10) -> int:
        """同步排空（最多 max_items 条），给 guard --once 用。"""
        store = self._open()
        done = 0
        try:
            while done < max(0, int(max_items)) and self.run_once(store):
                done += 1
        finally:
            store.close()
        return done

    def run(self) -> None:
        store = self._open()
        last_maintenance = 0.0
        try:
            while not self._stop.is_set():
                try:
                    worked = self.run_once(store)
                except Exception as exc:  # noqa: BLE001 - 数据库/线程异常不能让 worker 退出
                    log.warning("outbox worker 本轮异常: %s", exc)
                    worked = False
                now = time.monotonic()
                if now - last_maintenance >= self.maintenance_seconds:
                    last_maintenance = now
                    try:
                        store.expire_and_prune(
                            media_dir=self.media_dir,
                            max_age_days=self.max_age_days,
                            max_total_bytes=self.max_total_bytes,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("outbox 清理失败: %s", exc)
                if not worked:
                    self._stop.wait(self.poll_seconds)
        finally:
            store.close()


__all__ = [
    "KNOWN_CHANNELS", "channel_list", "enabled_channels", "supports_image",
    "supports_video", "build_items", "ChannelDispatcher", "DeliveryWorker",
]
