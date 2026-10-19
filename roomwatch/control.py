"""停止协议：界面/命令行怎么让监控进程「体面地」退出。

为什么不是 `taskkill /F`
------------------------
强杀有三个后果，都在 0.1.7 真机上出现过：
1. 正在写的 mp4 头没写完，相册里点开是坏的；
2. 音轨没合并，那段录像没声音；
3. `voice.ensure_system_volume` 抬高过的主音量不会还原（用户系统音量停在 70%）。

所以停止分两段：
- **优雅**：写一个 `var/state/stop.request`（带目标 PID），watcher 主循环
  每秒查一次，命中就退出循环，走它本来就有的 `finally` 收尾；
- **兜底**：等 `grace` 秒还没退，才由调用方决定要不要强杀。

请求文件里带 PID 是必要的：PID 会被复用，残留的请求不能秒杀刚起来的新进程。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import health
from . import procutil

__all__ = [
    "STOP_NAME", "DESIRED_NAME", "GALLERY_PID_NAME",
    "stop_request_path", "read_stop_request", "request_stop",
    "clear_stop_request", "stop_requested", "stop_watcher", "DEFAULT_GRACE",
    "desired_state_path", "read_desired_state", "set_desired_state",
    "gallery_pid_path", "pid_matches", "stop_gallery",
]

STOP_NAME = "stop.request"
DESIRED_NAME = "desired_state.json"
GALLERY_PID_NAME = "gallery.pid"
DEFAULT_GRACE = 20.0     # 录像收尾 + 音轨合并，实测 2~8 秒；留足余量


def stop_request_path(root: Path | str) -> Path:
    return Path(root) / "var" / "state" / STOP_NAME


def desired_state_path(root: Path | str) -> Path:
    return Path(root) / "var" / "state" / DESIRED_NAME


def gallery_pid_path(root: Path | str) -> Path:
    return Path(root) / "var" / "state" / GALLERY_PID_NAME


def read_desired_state(root: Path | str) -> str:
    """用户期望监控处于 running 还是 paused。

    文件缺失/损坏一律按 running：宁可多拉起一次，也不要让监控在用户
    不知情的情况下永久待机。
    """
    try:
        data = json.loads(desired_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "running"
    if isinstance(data, dict) and data.get("state") in ("running", "paused"):
        return str(data["state"])
    return "running"


def set_desired_state(root: Path | str, state: str, *, by: str = "unknown") -> Path:
    if state not in ("running", "paused"):
        raise ValueError(f"未知的期望状态：{state!r}（只支持 running / paused）")
    path = desired_state_path(root)
    health.write_json_atomic(path, {
        "state": state,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "by": by,
    })
    return path


def pid_matches(
    pid: int,
    markers: tuple[str, ...],
    *,
    cmdline: Optional[Callable[[int], list[str]]] = None,
) -> bool:
    """PID 还活着不够，命令行里必须出现全部标记，防止 PID 复用误判。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        args = cmdline(pid) if cmdline else _psutil_cmdline(pid)
    except Exception:  # noqa: BLE001 - 进程刚死 / 权限不足都按不匹配处理
        return False
    if not args:
        return False
    text = " ".join(str(part) for part in args).lower()
    return all(marker.lower() in text for marker in markers)


def _psutil_cmdline(pid: int) -> list[str]:
    try:
        import psutil

        return list(psutil.Process(int(pid)).cmdline())
    except Exception:  # noqa: BLE001
        return []


def request_stop(root: Path | str, pid: int) -> Path:
    """落一份停止请求。原子写，避免 watcher 读到半截 JSON。"""
    path = stop_request_path(root)
    health.write_json_atomic(path, {
        "pid": int(pid),
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    })
    return path


def read_stop_request(root: Path | str) -> Optional[dict]:
    path = stop_request_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_stop_request(root: Path | str) -> None:
    path = stop_request_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def stop_requested(root: Path | str, pid: int) -> bool:
    """这份请求是不是发给当前这个 PID 的。"""
    data = read_stop_request(root)
    if not data:
        return False
    try:
        return int(data.get("pid") or 0) == int(pid)
    except (TypeError, ValueError):
        return False


def stop_watcher(
    root: Path | str,
    pid: int,
    *,
    grace: float = DEFAULT_GRACE,
    force: bool = True,
    alive: Optional[Callable[[int], bool]] = None,
    kill: Optional[Callable[..., bool]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> dict:
    """两段式停止。返回 {"method": ..., "waited": 秒}。

    method:
      already-stopped  本来就不在跑
      graceful         它自己收尾退出了
      forced           超过 grace 后强杀成功
      timeout          超过 grace 且调用方不允许强杀
      stuck            强杀了还是没消失（极少见：驱动卡在 D 状态）

    alive/kill/sleep 可注入，单测里绝不真的杀进程。
    """
    alive = alive or procutil.pid_alive
    kill = kill or procutil.kill_pid
    sleep = sleep or time.sleep

    # 先落请求再判活：顺序反了的话，进程在两次检查之间退出的窗口里
    # 我们既没通知到它、也不会走优雅路径。
    if pid and pid > 0:
        request_stop(root, pid)

    if not pid or not alive(pid):
        clear_stop_request(root)
        return {"method": "already-stopped", "waited": 0.0}

    started = time.monotonic()
    deadline = started + max(0.0, float(grace))
    while time.monotonic() < deadline:
        if not alive(pid):
            break
        sleep(procutil.POLL_SECONDS)

    waited = round(time.monotonic() - started, 1)
    if not alive(pid):
        clear_stop_request(root)
        return {"method": "graceful", "waited": waited}

    if not force:
        return {"method": "timeout", "waited": waited}

    kill(pid)
    if alive(pid):
        return {"method": "stuck", "waited": waited}
    clear_stop_request(root)
    return {"method": "forced", "waited": waited}


def stop_gallery(
    root: Path | str,
    *,
    alive: Optional[Callable[[int], bool]] = None,
    kill: Optional[Callable[..., bool]] = None,
    cmdline: Optional[Callable[[int], list[str]]] = None,
) -> dict:
    """按 PID 文件停止相册；命令行不匹配视为已停止，绝不误杀复用 PID。"""
    alive = alive or procutil.pid_alive
    kill = kill or procutil.kill_pid

    try:
        pid = int(gallery_pid_path(root).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        pid = 0

    if (
        not pid
        or not alive(pid)
        or not pid_matches(pid, ("roomwatch", "gallery"), cmdline=cmdline)
    ):
        return {"method": "already-stopped", "waited": 0.0}

    if kill(pid, force=False):
        return {"method": "graceful", "waited": 0.0}
    if kill(pid, force=True):
        return {"method": "forced", "waited": 0.0}
    return {"method": "stuck", "waited": 0.0}
