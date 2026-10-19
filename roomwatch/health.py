"""健康快照、退出码与退出记录。

这一层只做文件与时间，不依赖 cv2、摄像头或网络，方便离线单测。
watcher 每秒写一次健康快照，guardian 与 `rw diag` 读它来回答
「进程还在吗、为什么退出、上次跑到哪一帧」。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "EXIT_OK", "EXIT_CONFIG", "EXIT_CAMERA", "EXIT_MODEL",
    "EXIT_UNHANDLED", "EXIT_STALLED",
    "write_json_atomic", "read_json", "build_health_payload", "proc_stats",
    "write_health", "write_exit_record", "read_exit_record",
    "stall_exceeded", "watchdog_loop",
]

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CAMERA = 3
EXIT_MODEL = 4
EXIT_UNHANDLED = 5
EXIT_STALLED = 6


_ATOMIC_LOCK = threading.Lock()


def write_json_atomic(path: str | Path, payload: dict) -> None:
    """同目录写临时文件再替换，读侧永远不会看到半截 JSON。

    临时文件名必须唯一：GUI 与 CLI 可能同时写 desired_state.json，
    固定 `.tmp` 名会让两个进程互相覆盖/删除，最终抛 PermissionError。
    同进程内再加一把锁，避免多个线程同时替换同一目标。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with _ATOMIC_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            for attempt in range(5):
                try:
                    os.replace(tmp_name, target)
                    break
                except PermissionError:
                    # Windows：目标恰好被读取/替换时可能瞬时报占用，重试即可
                    if attempt == 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def read_json(path: str | Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


_PROCESS = None
_PROCESS_PID: Optional[int] = None


def _process():
    """复用同一个 psutil.Process；否则 cpu_percent 每次都从基线开始，永远是 0。"""
    global _PROCESS, _PROCESS_PID
    import psutil

    pid = os.getpid()
    if _PROCESS is None or _PROCESS_PID != pid:
        _PROCESS = psutil.Process(pid)
        _PROCESS_PID = pid
        _PROCESS.cpu_percent(interval=None)  # 建立基线
    return _PROCESS


def proc_stats() -> tuple[Optional[float], Optional[float]]:
    """当前进程的 RSS(MB) 与 CPU 百分比；拿不到就返回 (None, None)。"""
    try:
        proc = _process()
        rss_mb = proc.memory_info().rss / (1024 * 1024)
        cpu_pct = proc.cpu_percent(interval=None)
        return round(float(rss_mb), 1), round(float(cpu_pct), 1)
    except Exception:  # noqa: BLE001 - 健康采集绝不能影响主循环
        return None, None


def build_health_payload(
    *,
    pid: int,
    loop_seq: int,
    started_at,
    last_frame_at,
    last_inference_at,
    consecutive_errors: int,
    camera_reopens: int,
    selfcheck: Optional[list[dict]] = None,
    extra: Optional[dict] = None,
) -> dict:
    rss_mb, cpu_pct = proc_stats()
    payload = {
        "pid": int(pid),
        "loop_seq": int(loop_seq),
        "started_at": _iso(started_at),
        "last_frame_at": _iso(last_frame_at),
        "last_inference_at": _iso(last_inference_at),
        "consecutive_errors": int(consecutive_errors),
        "camera_reopens": int(camera_reopens),
        "rss_mb": rss_mb,
        "cpu_pct": cpu_pct,
        "selfcheck": selfcheck or [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        payload.update(extra)
    return payload


def write_health(path: str | Path, payload: dict) -> None:
    write_json_atomic(path, payload)


def write_exit_record(
    path: str | Path,
    *,
    pid: int,
    code: int,
    reason: str,
    at: Optional[str] = None,
) -> None:
    write_json_atomic(path, {
        "pid": int(pid),
        "code": int(code),
        "reason": str(reason),
        "at": at or datetime.now().isoformat(timespec="seconds"),
    })


def read_exit_record(path: str | Path) -> Optional[dict]:
    return read_json(path)


def stall_exceeded(loop_seq: int, last_seq: int, unchanged_seconds: float, threshold: float) -> bool:
    """主循环序号没变、且停滞时间严格超过阈值。"""
    return int(loop_seq) == int(last_seq) and float(unchanged_seconds) > float(threshold)


def watchdog_loop(
    *,
    get_seq: Callable[[], int],
    dump_stacks: Callable[[], None],
    on_stall: Callable[[], None],
    sleep: Callable[[float], None],
    stop_event,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 5.0,
    threshold_seconds: float = 120.0,
) -> None:
    """看门狗线程体：序号停滞超过阈值时先转储线程栈，再交给调用方处理。"""
    last_seq = int(get_seq())
    last_change = float(clock())
    while not stop_event.is_set():
        sleep(poll_seconds)
        if stop_event.is_set():
            return
        seq = int(get_seq())
        now = float(clock())
        if seq != last_seq:
            last_seq = seq
            last_change = now
            continue
        if stall_exceeded(seq, last_seq, now - last_change, threshold_seconds):
            dump_stacks()
            on_stall()
            return
