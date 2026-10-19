"""外部心跳（死手开关）。

整机断电、断网、guardian 自己也死掉时，进程内的一切通知都发不出去。
唯一可靠的办法是让外部服务反向发现「心跳停了」：

- 健康时按间隔 GET/POST 用户配置的成功 URL（如 healthchecks.io）；
- 长时间不健康时，如果配置了失败 URL 就 ping 它，否则停止 ping，
  由外部服务的宽限期告警；
- 用户暂停监控时不 ping（这是预期行为，文档会说明）；
- URL 由用户显式填写，不拼接任何服务商特定后缀。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import requests

log = logging.getLogger("room-watch")


@dataclass(frozen=True)
class HeartbeatConfig:
    url: str = ""
    fail_url: str = ""
    start_url: str = ""
    interval_seconds: float = 300.0
    timeout_seconds: float = 10.0

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def has_fail_url(self) -> bool:
        return bool(self.fail_url)


def _clean_url(value) -> str:
    text = str(value or "").strip()
    if not text or "replace_me" in text.lower():
        return ""
    return text


def load_heartbeat(cfg: dict) -> HeartbeatConfig:
    block = (cfg or {}).get("monitor") or {}
    try:
        interval = float(block.get("heartbeat_interval_seconds", 300) or 300)
    except (TypeError, ValueError):
        interval = 300.0
    try:
        timeout = float(block.get("heartbeat_timeout_seconds", 10) or 10)
    except (TypeError, ValueError):
        timeout = 10.0
    return HeartbeatConfig(
        url=_clean_url(block.get("heartbeat_url")),
        fail_url=_clean_url(block.get("heartbeat_fail_url")),
        start_url=_clean_url(block.get("heartbeat_start_url")),
        interval_seconds=max(30.0, interval),
        timeout_seconds=max(1.0, timeout),
    )


def heartbeat_due(
    last_at: Optional[str], now: datetime, interval_seconds: float
) -> bool:
    if not last_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_at))
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= max(0.0, float(interval_seconds))


def ping(url: str, timeout: float = 10.0, http=requests.get) -> tuple[bool, str]:
    """发一次心跳。任何异常都不许抛给守护进程。"""
    if not url:
        return False, "URL 为空"
    try:
        response = http(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络问题只记录
        return False, str(exc)
    code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= code < 300:
        return True, ""
    return False, f"HTTP {code}"


class HeartbeatSender:
    """把心跳状态写进 guardian state，便于 GUI 显示最近结果。"""

    def __init__(
        self,
        config: HeartbeatConfig,
        state: dict,
        save_state: Callable[[], None],
        *,
        http=requests.get,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.config = config
        self.state = state
        self.save_state = save_state
        self.http = http
        self.clock = clock

    def note_start(self) -> None:
        if not self.config.start_url:
            return
        ok, error = ping(self.config.start_url, self.config.timeout_seconds, self.http)
        self.state["last_heartbeat_start_at"] = self.clock().isoformat(timespec="seconds")
        self.save_state()
        if not ok:
            log.warning("启动心跳失败：%s", error)

    @property
    def failure_streak(self) -> int:
        try:
            return int(self.state.get("heartbeat_fail_streak") or 0)
        except (TypeError, ValueError):
            return 0

    def maybe_send(
        self,
        *,
        healthy: bool,
        paused: bool,
        now: Optional[datetime] = None,
    ) -> str:
        """按状态发一次心跳，返回动作字符串（便于日志/测试）。"""
        if not self.config.configured:
            return "disabled"
        now = now or self.clock()
        if paused:
            # 用户主动暂停：停止 ping 是预期行为，外部服务宽限期后会提醒。
            return "paused"

        if healthy:
            streak = self.failure_streak
            if streak == 0 and not heartbeat_due(
                self.state.get("last_heartbeat_at"), now,
                self.config.interval_seconds,
            ):
                return "skip"
            ok, error = ping(self.config.url, self.config.timeout_seconds, self.http)
            self.state["last_heartbeat_at"] = now.isoformat(timespec="seconds")
            self.state["last_heartbeat_ok"] = bool(ok)
            self.state["last_heartbeat_error"] = "" if ok else str(error)[:500]
            self.state["heartbeat_fail_streak"] = 0
            self.save_state()
            if ok:
                log.info("外部心跳成功")
                return "ok"
            log.warning("外部心跳失败：%s", error)
            return "fail"

        # 不健康：有失败 URL 就按间隔 ping 它；没有就停止 ping，让外部服务告警。
        self.state["heartbeat_fail_streak"] = self.failure_streak + 1
        if not self.config.has_fail_url:
            self.state["last_heartbeat_error"] = "监控不健康，已停止心跳"
            self.save_state()
            return "stopped"

        if not heartbeat_due(
            self.state.get("last_heartbeat_fail_at"), now,
            self.config.interval_seconds,
        ):
            self.save_state()
            return "fail-skip"
        ok, error = ping(self.config.fail_url, self.config.timeout_seconds, self.http)
        self.state["last_heartbeat_fail_at"] = now.isoformat(timespec="seconds")
        self.state["last_heartbeat_ok"] = False
        self.state["last_heartbeat_error"] = "" if ok else str(error)[:500]
        self.save_state()
        if ok:
            log.warning("外部失败心跳已发送")
            return "fail"
        log.warning("外部失败心跳发送失败：%s", error)
        return "fail-error"


__all__ = [
    "HeartbeatConfig", "load_heartbeat", "heartbeat_due", "ping",
    "HeartbeatSender",
]
