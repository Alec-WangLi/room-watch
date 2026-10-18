"""进程判活、等待退出、结束进程 —— 三件与「它还在不在」有关的事。

为什么要单独一层
----------------
这三件事原来各写各的：`updater._pid_alive` 用 `tasklist`，
`gui.read_state` 干脆不看进程只看心跳（于是进程被杀后 120 秒内界面仍说在线，
用户表现为「停止后点不了启动监控」）。判活逻辑一旦分叉，行为就会漂移，
而漂移的代价是用户以为程序挂了或没挂。所以统一到这里。

`psutil` 优先：它直接问系统，不受控制台编码影响；
没装时退回 `tasklist`（Windows）/ `os.kill(pid, 0)`（类 Unix）。
"""

from __future__ import annotations

import os
import re
import subprocess
import time

__all__ = ["pid_alive", "wait_pid_exit", "kill_pid", "POLL_SECONDS"]

# 轮询步长。0.2 秒在「人手感觉」上足够快，又不会把 CPU 吃出可见占用。
POLL_SECONDS = 0.2


def _psutil_module():
    try:
        import psutil  # noqa: PLC0415 - 可选依赖，按需探测
    except Exception:  # noqa: BLE001
        return None
    return psutil


def pid_alive(pid: int) -> bool:
    """这个 PID 现在还有进程吗。非法 PID 一律 False。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    psutil = _psutil_module()
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001 - psutil 在极端情况下会抛 AccessDenied
            pass

    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        out = proc.stdout or b""
        # 不整串 find(pid)：内存列里可能出现同样的数字（"12,340 K"），会假阳性
        return re.search(rb"(?<!\d)" + str(pid).encode("ascii") + rb"(?!\d)", out) is not None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 进程在，只是不归我们管
    except OSError:
        return False
    return True


def wait_pid_exit(pid: int, *, timeout: float = 30.0) -> bool:
    """等它退出，返回「是否已经不在」。忙等，步长 POLL_SECONDS。"""
    if not pid_alive(pid):
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(POLL_SECONDS)
    return not pid_alive(pid)


def kill_pid(pid: int, *, timeout: float = 5.0, force: bool = True) -> bool:
    """结束进程并等它真的消失。返回「结束后已不在」。

    force=True 走 `taskkill /F`（Windows）或 SIGKILL，**会丢掉正在写的 mp4**。
    所以调用方必须先走优雅停止（control.stop_watcher），这里只做兜底。
    """
    if not pid_alive(pid):
        return True
    try:
        if os.name == "nt":
            cmd = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
            subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        else:
            import signal

            os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return wait_pid_exit(pid, timeout=timeout)
