"""持久化发件箱：通知先落盘，再由后台 worker 投递。

为什么需要它
------------
0.1.15 之前，通知是「在事件现场直接发 HTTP」。断网、进程被杀、渠道抽风，
那条告警就永远没了——而产品最核心的承诺是「真出事时一定通知到」。
所以改成：

    事件 → outbox（SQLite）→ worker → 渠道

主循环只入队，不等待网络；发送失败按指数退避重试；进程重启后继续投递。

状态机
------
    pending → sending → delivered
                      ↘ pending（可重试失败，带 next_attempt_at）
                      ↘ failed_permanent（凭据/配置错误，不再重试）
    超过保留期 → expired

并发
----
watcher 与 guardian 都会启动 worker，SQLite 用 WAL + `BEGIN IMMEDIATE`
抢占任务；`sending` 带租约，进程崩溃后 `recover_stale()` 会把超时的项
放回 pending，避免永远卡在「发送中」。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed_permanent"
STATUS_EXPIRED = "expired"

SEVERITY_ORDER = {"critical": 0, "event": 1, "digest": 2}
KIND_ORDER = {"text": 0, "image": 1, "video": 2}

# 第一次失败等 5 秒，之后 30 秒、2 分、10 分、1 小时、6 小时，封顶 24 小时。
# attempts 表示「已经失败过几次」，所以 attempts=0 时取第一个值。
RETRY_DELAYS = (5, 30, 120, 600, 3600, 21600, 86400)


def retry_delay(attempts: int) -> float:
    index = max(0, min(int(attempts), len(RETRY_DELAYS) - 1))
    return float(RETRY_DELAYS[index])


def _iso(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).isoformat(timespec="seconds")


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    retryable: bool = True
    error: str = ""


@dataclass(frozen=True)
class OutboxItem:
    id: int
    event_id: str
    channel: str
    kind: str
    severity: str
    payload: dict
    created_at: str
    attempts: int
    next_attempt_at: str
    status: str
    last_error: str
    dedupe_key: str


class OutboxStore:
    """一个进程一个连接；所有时间都用本地时间的 ISO 字符串。"""

    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS outbox (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   event_id TEXT NOT NULL DEFAULT '',
                   channel TEXT NOT NULL,
                   kind TEXT NOT NULL,
                   severity TEXT NOT NULL DEFAULT 'event',
                   payload TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending',
                   last_error TEXT NOT NULL DEFAULT '',
                   dedupe_key TEXT UNIQUE,
                   lease_until TEXT,
                   delivered_at TEXT
               )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_due "
            "ON outbox(status, next_attempt_at)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS outbox_channels (
                   channel TEXT PRIMARY KEY,
                   last_ok_at TEXT,
                   last_error TEXT NOT NULL DEFAULT '',
                   last_error_at TEXT
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS outbox_meta (
                   key TEXT PRIMARY KEY,
                   value TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        self.conn.commit()

    # ------------------------------------------------------------ 入队

    def enqueue(
        self,
        *,
        channel: str,
        kind: str,
        severity: str = "event",
        payload: dict,
        event_id: str = "",
        dedupe_key: str = "",
        delay_seconds: float = 0.0,
        now: Optional[datetime] = None,
    ) -> Optional[int]:
        """返回新行 id；dedupe_key 已存在时返回 None（幂等）。

        `delay_seconds` 用于「先压住、等身份确认」的事件通知：行仍然是
        pending，但 worker 要到期后才会领取。
        """
        created = now or datetime.now()
        ready = created + timedelta(seconds=max(0.0, float(delay_seconds)))
        key = dedupe_key or None
        try:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO outbox "
                "(event_id, channel, kind, severity, payload, created_at, next_attempt_at, dedupe_key) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(event_id or ""),
                    str(channel),
                    str(kind),
                    str(severity),
                    json.dumps(payload, ensure_ascii=False),
                    _iso(created),
                    _iso(ready),
                    key,
                ),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return int(cur.lastrowid) if cur.rowcount else None

    def enqueue_many(self, items: Iterable[dict], *, delay_seconds: float = 0.0,
                     now: Optional[datetime] = None) -> int:
        """批量入队，返回真正新增的条数（重复的去重键不算）。"""
        added = 0
        for item in items:
            new_id = self.enqueue(
                channel=item["channel"],
                kind=item["kind"],
                severity=item.get("severity", "event"),
                payload=item.get("payload") or {},
                event_id=item.get("event_id", ""),
                dedupe_key=item.get("dedupe_key", ""),
                delay_seconds=delay_seconds,
                now=now,
            )
            if new_id is not None:
                added += 1
        return added

    def release_event(self, event_id: str, *, now: Optional[datetime] = None) -> int:
        """把某事件所有尚未到期的 pending 项立刻变为可投递。

        用于「已经看清是陌生人，不用等满等待窗口」以及窗口到期。
        """
        moment = _iso(now)
        try:
            cur = self.conn.execute(
                "UPDATE outbox SET next_attempt_at=? "
                "WHERE event_id=? AND status=? AND next_attempt_at > ?",
                (moment, str(event_id), STATUS_PENDING, moment),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return int(cur.rowcount)

    def cancel_event(self, event_id: str) -> dict:
        """撤销某事件尚未投递的通知，返回各状态的数量。

        已经 delivered / sending 的项不能撤回（消息已经离开发件箱）；
        调用方据此判断「是否还来得及做到什么动作都不做」。
        """
        try:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM outbox WHERE event_id=? GROUP BY status",
                (str(event_id),),
            ).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
            self.conn.execute(
                "DELETE FROM outbox WHERE event_id=? AND status=?",
                (str(event_id), STATUS_PENDING),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return {
            "pending": counts.get(STATUS_PENDING, 0),
            "sending": counts.get(STATUS_SENDING, 0),
            "delivered": counts.get(STATUS_DELIVERED, 0),
            "failed_permanent": counts.get(STATUS_FAILED, 0),
            "expired": counts.get(STATUS_EXPIRED, 0),
        }

    # ------------------------------------------------------------ 领取

    def claim_next(
        self,
        *,
        now: Optional[datetime] = None,
        lease_seconds: float = 120.0,
        kinds: Optional[tuple] = None,
    ) -> Optional[OutboxItem]:
        now_dt = now or datetime.now()
        self.recover_stale(now=now_dt)
        params: list = [_iso(now_dt)]
        where = "status = ? AND next_attempt_at <= ?"
        params.insert(0, STATUS_PENDING)
        if kinds:
            marks = ",".join("?" for _ in kinds)
            where += f" AND kind IN ({marks})"
            params.extend(kinds)
        order = (
            "CASE severity WHEN 'critical' THEN 0 WHEN 'event' THEN 1 ELSE 2 END, "
            "CASE kind WHEN 'text' THEN 0 WHEN 'image' THEN 1 ELSE 2 END, id"
        )
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                f"SELECT * FROM outbox WHERE {where} ORDER BY {order} LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            lease = _iso(now_dt + timedelta(seconds=max(1.0, float(lease_seconds))))
            self.conn.execute(
                "UPDATE outbox SET status = ?, lease_until = ? WHERE id = ? AND status = ?",
                (STATUS_SENDING, lease, row["id"], STATUS_PENDING),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return _row_to_item(row, status=STATUS_SENDING)

    def recover_stale(self, *, now: Optional[datetime] = None) -> int:
        """sending 超过租约时间 → 回到 pending（进程崩溃恢复）。"""
        cur = self.conn.execute(
            "UPDATE outbox SET status = ?, lease_until = NULL "
            "WHERE status = ? AND lease_until IS NOT NULL AND lease_until <= ?",
            (STATUS_PENDING, STATUS_SENDING, _iso(now)),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------ 结果

    def mark_delivered(self, item_id: int, *, now: Optional[datetime] = None) -> None:
        at = _iso(now)
        self.conn.execute(
            "UPDATE outbox SET status = ?, lease_until = NULL, delivered_at = ?, last_error = '' "
            "WHERE id = ?",
            (STATUS_DELIVERED, at, int(item_id)),
        )
        self.conn.execute(
            "INSERT INTO outbox_meta(key, value, updated_at) VALUES('last_success_at', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (at, at),
        )
        self.conn.commit()

    def mark_retry(
        self,
        item_id: int,
        *,
        error: str,
        retryable: bool = True,
        now: Optional[datetime] = None,
        jitter: float = 0.0,
    ) -> str:
        """记录一次失败，返回新的状态（pending 或 failed_permanent）。"""
        row = self.conn.execute(
            "SELECT attempts FROM outbox WHERE id = ?", (int(item_id),)
        ).fetchone()
        if row is None:
            return STATUS_FAILED
        attempts = int(row["attempts"] or 0)
        if not retryable:
            self.conn.execute(
                "UPDATE outbox SET status = ?, attempts = ?, lease_until = NULL, last_error = ? "
                "WHERE id = ?",
                (STATUS_FAILED, attempts + 1, str(error)[:2000], int(item_id)),
            )
            self.conn.commit()
            return STATUS_FAILED

        delay = retry_delay(attempts)
        if jitter > 0:
            delay *= 1.0 + random.uniform(-jitter, jitter)
        attempt_at = (now or datetime.now()) + timedelta(seconds=max(1.0, delay))
        self.conn.execute(
            "UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?, "
            "lease_until = NULL, last_error = ? WHERE id = ?",
            (STATUS_PENDING, attempts + 1, _iso(attempt_at), str(error)[:2000], int(item_id)),
        )
        self.conn.commit()
        return STATUS_PENDING

    # ------------------------------------------------------------ 渠道健康

    def record_channel_result(
        self,
        channel: str,
        ok: bool,
        error: str = "",
        *,
        now: Optional[datetime] = None,
    ) -> None:
        at = _iso(now)
        if ok:
            self.conn.execute(
                "INSERT INTO outbox_channels(channel, last_ok_at, last_error, last_error_at) "
                "VALUES (?,?, '', NULL) "
                "ON CONFLICT(channel) DO UPDATE SET last_ok_at=excluded.last_ok_at, last_error='', "
                "last_error_at=NULL",
                (str(channel), at),
            )
        else:
            self.conn.execute(
                "INSERT INTO outbox_channels(channel, last_ok_at, last_error, last_error_at) "
                "VALUES (?, NULL, ?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at",
                (str(channel), str(error)[:500], at),
            )
        self.conn.commit()

    def channel_health(self) -> dict:
        rows = self.conn.execute("SELECT * FROM outbox_channels").fetchall()
        return {
            str(row["channel"]): {
                "last_ok_at": row["last_ok_at"],
                "last_error": row["last_error"] or "",
                "last_error_at": row["last_error_at"],
            }
            for row in rows
        }

    # ------------------------------------------------------------ 清理

    def _delete_media(self, payload: dict, media_dir: Path) -> int:
        path = payload.get("outbox_path")
        if not path:
            return 0
        try:
            target = Path(str(path)).resolve()
            root = media_dir.resolve()
            if root not in target.parents and target != root:
                return 0          # 只删 outbox 自己的临时文件
            if target.is_file():
                size = target.stat().st_size
                target.unlink()
                return size
        except OSError:
            pass
        return 0

    def expire_and_prune(
        self,
        *,
        media_dir: str | Path,
        max_age_days: float = 7,
        max_total_bytes: int = 2 * 1024 ** 3,
        now: Optional[datetime] = None,
    ) -> dict:
        now_dt = now or datetime.now()
        media = Path(media_dir)
        cutoff = _iso(now_dt - timedelta(days=max(0.0, float(max_age_days))))
        expired = 0
        deleted = 0
        freed = 0

        rows = self.conn.execute(
            "SELECT id, payload FROM outbox WHERE status IN (?,?) AND created_at < ?",
            (STATUS_PENDING, STATUS_FAILED, cutoff),
        ).fetchall()
        for row in rows:
            payload = _payload(row["payload"])
            freed += self._delete_media(payload, media)
            self.conn.execute(
                "UPDATE outbox SET status = ? WHERE id = ?", (STATUS_EXPIRED, row["id"])
            )
            expired += 1

        old = self.conn.execute(
            "SELECT id, payload FROM outbox WHERE status = ? AND created_at < ?",
            (STATUS_DELIVERED, cutoff),
        ).fetchall()
        for row in old:
            payload = _payload(row["payload"])
            freed += self._delete_media(payload, media)
            self.conn.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
            deleted += 1
        self.conn.commit()

        # 容量上限：先淘汰最旧的 video，再 image；永远不淘汰 text。
        used = _dir_size(media)
        if used > max_total_bytes:
            for kind in ("video", "image"):
                if used <= max_total_bytes:
                    break
                rows = self.conn.execute(
                    "SELECT id, payload FROM outbox WHERE status IN (?,?) AND kind = ? "
                    "ORDER BY created_at ASC",
                    (STATUS_PENDING, STATUS_FAILED, kind),
                ).fetchall()
                for row in rows:
                    if used <= max_total_bytes:
                        break
                    payload = _payload(row["payload"])
                    freed += self._delete_media(payload, media)
                    self.conn.execute(
                        "UPDATE outbox SET status = ?, last_error = ? WHERE id = ?",
                        (STATUS_EXPIRED, "队列容量上限，淘汰旧媒体", row["id"]),
                    )
                    expired += 1
                    used = _dir_size(media)
            self.conn.commit()

        # 孤儿临时文件（转码成功但进程在发送前被杀）也会随时间清理，
        # 避免 var/outbox 慢慢变大。
        cutoff_dt = now_dt - timedelta(days=max(0.0, float(max_age_days)))
        try:
            for entry in media.rglob("*"):
                if not entry.is_file():
                    continue
                try:
                    if datetime.fromtimestamp(entry.stat().st_mtime) < cutoff_dt:
                        size = entry.stat().st_size
                        entry.unlink()
                        freed += size
                except OSError:
                    continue
        except OSError:
            pass

        return {
            "expired": expired,
            "deleted": deleted,
            "bytes_freed": freed,
        }

    # ------------------------------------------------------------ 查询

    def stats(self) -> dict:
        counts = {status: 0 for status in (
            STATUS_PENDING, STATUS_SENDING, STATUS_DELIVERED,
            STATUS_FAILED, STATUS_EXPIRED,
        )}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["n"])
        oldest = self.conn.execute(
            "SELECT MIN(created_at) FROM outbox WHERE status IN (?,?)",
            (STATUS_PENDING, STATUS_SENDING),
        ).fetchone()[0]
        return {
            **counts,
            "pending": counts[STATUS_PENDING] + counts[STATUS_SENDING],
            "oldest_pending_at": oldest,
            "last_success_at": self.last_success_at(),
        }

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status IN (?,?)",
            (STATUS_PENDING, STATUS_SENDING),
        ).fetchone()
        return int(row[0] or 0)

    def last_success_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM outbox_meta WHERE key = 'last_success_at'"
        ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def _payload(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row_to_item(row: sqlite3.Row, *, status: Optional[str] = None) -> OutboxItem:
    return OutboxItem(
        id=int(row["id"]),
        event_id=str(row["event_id"] or ""),
        channel=str(row["channel"]),
        kind=str(row["kind"]),
        severity=str(row["severity"] or "event"),
        payload=_payload(row["payload"]),
        created_at=str(row["created_at"]),
        attempts=int(row["attempts"] or 0),
        next_attempt_at=str(row["next_attempt_at"]),
        status=str(status or row["status"]),
        last_error=str(row["last_error"] or ""),
        dedupe_key=str(row["dedupe_key"] or ""),
    )


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


__all__ = [
    "STATUS_PENDING", "STATUS_SENDING", "STATUS_DELIVERED",
    "STATUS_FAILED", "STATUS_EXPIRED",
    "DeliveryResult", "OutboxItem", "OutboxStore", "retry_delay",
]
