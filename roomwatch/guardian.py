"""守护进程：保证 watcher 一直活着。

为什么需要它
------------
watcher 内部已经有相当多的自愈能力（读帧失败重连、摄像头重开），
但有一类故障它救不了自己：**进程本身死掉**。
- Python 抛了未捕获异常
- 被系统 OOM 掉
- 显存/句柄耗尽
- 用户误关窗口

进程一死，摄像头就没人读了，而你可能正在外面开会。
所以需要**外面的一双眼睛**。

两种用法
--------
1. `rw guard --once` —— 只检查一次就退出（幂等）。
   配 Windows 计划任务每 3 分钟跑一次，这是最可靠的方式：
   守护者本身不需要常驻，操作系统就是它的守护者。
2. `rw guard` —— 常驻模式，每 check_seconds 检查一次。

判据（这里最容易做错）
----------------------
「进程在不在」和「它是不是还活着（在干活）」是两件事：

| 现象 | 判据 | 处理 |
|---|---|---|
| 正常 | PID 存活 且 心跳新鲜 | 什么都不做 |
| 卡死 | PID 存活 但心跳过期 | 先观察，连续确认后杀掉重启 |
| 崩溃 | PID 不存在 | 直接重启 |
| 状态文件缺失 | 没有 status.json | 有进程就等，没进程就重启 |

「卡死」为什么不立刻杀：摄像头重连、模型首次下载都会让心跳停顿一两分钟。
误杀一个正在自愈的进程，比等它两分钟更糟。所以要连续 N 次确认。

退避
----
如果重启后立刻又挂（比如摄像头被别的程序占了），无脑重启会变成
每分钟拉起一次的恶性循环，把日志刷爆还占用 CPU。
所以连续失败时间隔指数增长，最长 15 分钟一次。

并发保护
--------
常驻 guardian 和计划任务可能同时跑，两个都想重启就会撞车。
所以真正动手前要拿文件锁；拿不到说明别人在干活，本轮直接退出。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import control
from . import health
from . import logutil
from . import procutil
from .config import load_config as _merge_config
from .paths import PROJECT_ROOT, resolve

log = logging.getLogger("guardian")

# decide() 的返回值
ACT_OK = "ok"                    # 一切正常
ACT_WAIT = "wait"                # 疑似卡死，再观察一轮
ACT_RESTART_GONE = "restart_gone"      # 进程没了
ACT_RESTART_STALLED = "restart_stalled"  # 进程在但心跳停了
ACT_BACKOFF = "backoff"          # 在退避窗口内，先不动
ACT_PAUSED = "paused"            # 用户主动暂停，不许拉起
ACT_STARTED = "started"          # 首次启动（不是崩溃重启）

HERE = str(PROJECT_ROOT)


def _abs(path: str) -> str:
    return resolve(path)


def pid_alive(pid: Optional[int]) -> bool:
    """PID 是否对应一个活着的进程。统一走 procutil，避免 Windows 上误杀。"""
    return procutil.pid_alive(pid)


def _psutil_cmdline(pid: int) -> list[str]:
    try:
        import psutil

        return list(psutil.Process(int(pid)).cmdline())
    except Exception:  # noqa: BLE001
        return []


def _cmdline_has(pid: int, markers: tuple[str, ...] = ("guard",)) -> bool:
    text = " ".join(_psutil_cmdline(pid)).lower()
    return all(marker.lower() in text for marker in markers)


def read_status(path: str) -> Optional[dict]:
    """读状态文件。读不到/坏了都返回 None，不能抛异常把守护者自己搞挂。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def append_history(path: str | Path, record: dict) -> None:
    """追加一条重启历史。写失败只记日志，不影响守护。"""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("写守护历史失败: %s", exc)


def read_history(path: str | Path, limit: int = 20) -> list[dict]:
    """读最近 limit 条历史；坏行跳过，文件不存在返回空列表。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit <= 0:
        return []
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def frame_age_seconds(status: Optional[dict], now: Optional[datetime] = None) -> Optional[float]:
    """最后一次成功读帧距现在多少秒。"""
    if not status:
        return None
    raw = status.get("last_frame_at")
    if not raw:
        return None
    try:
        then = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now()) - then).total_seconds()


def decide(
    status: Optional[dict],
    now: datetime,
    stale_seconds: float,
    stall_streak: int,
    stall_confirmations: int,
) -> str:
    """该做什么。纯函数，把所有分支都摆出来便于测试。

    stall_streak 是「此前已连续判定卡死几次」。
    """
    if status is None:
        # 状态文件都没有：可能是第一次启动，也可能是进程早死了。
        # 这时不能凭「文件缺失」下结论，交给调用方结合 PID 判断。
        return ACT_WAIT

    pid = status.get("pid")
    age = frame_age_seconds(status, now)

    if not pid_alive(pid):
        return ACT_RESTART_GONE

    if age is None:
        # 有心跳字段但解析不出来，按卡死算——总比不管强
        return ACT_RESTART_STALLED

    if age <= stale_seconds:
        return ACT_OK

    # 心跳过期但进程还在：再确认几次，避免误杀正在自愈的进程
    if stall_streak + 1 < max(1, stall_confirmations):
        return ACT_WAIT
    return ACT_RESTART_STALLED


def backoff_seconds(consecutive_failures: int, cap: float = 900.0) -> float:
    """连续失败 n 次后该等多久再试。

    0 次 → 立刻（第一次重启不必等）
    1 次 → 60 秒，2 次 → 120，3 次 → 240……封顶 cap。
    """
    if consecutive_failures <= 0:
        return 0.0
    return min(cap, 60.0 * (2 ** (consecutive_failures - 1)))


def decide_gallery(pid: Optional[int], alive, *, cmdline=None) -> str:
    """相册进程该怎么处理。

    只看 PID 不够：PID 会被复用。调用方提供 cmdline 时，命令行里必须同时
    出现 roomwatch 与 gallery，否则按「已死」处理，避免把别的进程当相册。
    """
    if not pid or not alive(pid):
        return ACT_RESTART_GONE
    if cmdline is not None:
        try:
            args = cmdline(pid)
        except Exception:  # noqa: BLE001
            return ACT_RESTART_GONE
        text = " ".join(str(part) for part in (args or [])).lower()
        if "roomwatch" not in text or "gallery" not in text:
            return ACT_RESTART_GONE
    return ACT_OK


def crash_window_count(timestamps: list[str], now: datetime, window_minutes: float) -> int:
    """统计窗口内的重启次数；坏时间与未来时间都跳过。"""
    window = float(window_minutes) * 60.0
    count = 0
    for raw in timestamps or []:
        try:
            then = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        delta = (now - then).total_seconds()
        if 0 <= delta <= window:
            count += 1
    return count


def _exit_summary(exit_record: Optional[dict]) -> str:
    rec = exit_record or {}
    reason = str(rec.get("reason") or "unknown")
    code = rec.get("code")
    return f"{reason}，退出码 {code}" if code is not None else reason


def recovery_text(service: str, exit_record: Optional[dict]) -> str:
    return f"room-watch：{service} 已恢复运行（上次退出：{_exit_summary(exit_record)}）"


def crash_alert_text(
    service: str, count: int, window_minutes: float, exit_record: Optional[dict]
) -> str:
    return (
        f"room-watch：{service} 在 {int(window_minutes)} 分钟内重启 {count} 次，"
        f"最近退出：{_exit_summary(exit_record)}。请检查摄像头与配置。"
    )


class FileLock:
    """极简的单实例锁：拿不到就说明别人在干活，本轮跳过。

    锁文件里写的是 PID，所以「上个进程崩了留下陈旧锁」能被识别并清理，
    否则守护者会永久失效——这是锁文件最常见的坑。
    """

    def __init__(self, path: str, validate=None):
        self.path = path
        self.validate = validate
        self.acquired = False

    def _stale(self) -> bool:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return True  # 读不出内容，当陈旧处理
        if not pid_alive(pid):
            return True
        if self.validate is not None:
            try:
                if not self.validate(pid):
                    return True
            except Exception:  # noqa: BLE001 - 校验失败按陈旧处理
                return True
        return False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._stale():
                return False
            try:
                os.remove(self.path)
            except OSError:
                return False
            return self.acquire()
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


class Guard:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run

        g = cfg.get("guardian") or {}
        self.check_seconds = float(g.get("check_seconds", 60))
        self.stale_seconds = float(g.get("stale_seconds", 300))
        self.stall_confirmations = int(g.get("stall_confirmations", 2))
        self.max_backoff = float(g.get("max_backoff_seconds", 900))
        self.lock_path = _abs(g.get("lock", "var/state/guardian.lock"))
        self.state_path = _abs(g.get("state", "var/state/guardian_state.json"))
        self.history_path = _abs(g.get("history", "var/state/guardian_history.jsonl"))
        health_cfg = cfg.get("health") or {}
        self.exit_record_path = _abs(
            health_cfg.get("exit_record") or "var/state/last_exit.json"
        )
        self.health_path = _abs(health_cfg.get("path") or "var/state/health.json")
        self.desired_state_path = _abs(
            g.get("desired_state", "var/state/desired_state.json")
        )
        self.root = Path(self.desired_state_path).parents[2]
        self.gallery_pid_path = _abs(g.get("gallery_pid", "var/state/gallery.pid"))
        self.gallery_log = _abs(g.get("gallery_log", "var/logs/gallery.log"))
        self.owner_lock_path = _abs(
            g.get("owner_lock", "var/state/guardian.owner.lock")
        )
        self.supervise_gallery = bool(g.get("supervise_gallery", True))
        self.notify_recovery = bool(g.get("notify_recovery", True))
        self.crash_window_minutes = float(g.get("crash_window_minutes", 10))
        self.crash_threshold = int(g.get("crash_threshold", 3))
        self.spawn_grace_seconds = float(g.get("spawn_grace_seconds", 120))
        self.kill_grace = float(g.get("kill_grace_seconds", 8.0))

        storage = cfg.get("storage") or {}
        self.status_path = _abs(storage.get("status", "status.json"))

        self.config_path = _abs(str(cfg.get("__config_path__", "config.yaml")))
        self.watcher_log = _abs(g.get("watcher_log", "watcher.log"))
        self.python = g.get("python") or sys.executable

        self.state = self._load_state()

    # ---------- 状态 ----------

    def _load_state(self) -> dict:
        base = {
            "restart_count": 0,
            "consecutive_failures": 0,
            "stall_streak": 0,
            "last_restart_at": None,
            "last_action": "init",
            "last_check_at": None,
            "pending_recovery": None,
            "restart_times": [],
            "last_crash_alert_at": None,
            "last_selfcheck_alert_at": None,
            "start_count": 0,
            "last_spawn_pid": None,
            "last_spawn_at": None,
            "last_desired_state": None,
            "gallery_failures": 0,
            "last_gallery_restart_at": None,
            "last_gallery_alert_at": None,
            "last_gallery_spawn_at": None,
            "last_gallery_spawn_pid": None,
        }
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                base.update({k: v for k, v in saved.items() if k in base or k.startswith("last_")})
        except (OSError, ValueError):
            pass
        return base

    def _save_state(self) -> None:
        self.state["last_check_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            health.write_json_atomic(self.state_path, self.state)
        except OSError as exc:
            log.warning("写守护状态失败: %s", exc)

    def _trusted_exit_record(self, pid: Optional[int] = None) -> Optional[dict]:
        record = health.read_exit_record(self.exit_record_path)
        if not record:
            return None
        if pid is not None:
            try:
                record_pid = int(record.get("pid") or 0)
            except (TypeError, ValueError):
                record_pid = 0
            if record_pid and record_pid != int(pid):
                return None
        return record

    def last_exit_reason(self, pid: Optional[int] = None) -> str:
        """上一次 watcher 退出原因；记录缺失、损坏或 PID 不符都按 unknown。"""
        record = self._trusted_exit_record(pid)
        if not record:
            return "unknown"
        return str(record.get("reason") or "unknown")

    # ---------- 动作 ----------

    def _restart_blocked(self, now: datetime) -> Optional[float]:
        """按窗口内的重启次数做指数退避；窗口外重启视为新故障，不等待。"""
        window = self.crash_window_minutes * 60.0
        recent: list[datetime] = []
        for raw in self.state.get("restart_times") or []:
            try:
                then = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            if 0 <= (now - then).total_seconds() <= window:
                recent.append(then)
        if not recent:
            return None
        wait = backoff_seconds(len(recent), self.max_backoff)
        elapsed = (now - max(recent)).total_seconds()
        if elapsed < wait:
            return wait - elapsed
        return None

    def _kill(self, pid: Optional[int]) -> None:
        if not pid:
            return
        try:
            import psutil

            proc = psutil.Process(int(pid))
            proc.terminate()
            try:
                proc.wait(timeout=self.kill_grace)
            except Exception:  # noqa: BLE001
                proc.kill()
            log.warning("已终止卡死进程 PID %s", pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("终止进程 %s 失败: %s", pid, exc)

    def _watcher_cmd(self) -> list[str]:
        """必须走 --foreground；`rw start` 的后台模式会再拉起一个 guardian。"""
        return [
            self.python,
            "-m",
            "roomwatch",
            "start",
            "--foreground",
            "--config",
            self.config_path,
            "--log",
            self.watcher_log,
        ]

    def _start_watcher(self) -> Optional[int]:
        """拉起 watcher。返回新 PID（拿不到就 None）。"""
        cmd = self._watcher_cmd()
        if self.dry_run:
            log.info("[dry-run] 本应执行: %s", " ".join(cmd))
            return None
        try:
            # DETACHED_PROCESS：让 watcher 脱离 guardian 的控制台独立运行。
            # 这样 guardian 自己退出（计划任务模式）时，watcher 不会被一起带走。
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            proc = subprocess.Popen(
                cmd,
                cwd=HERE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
            log.warning("已拉起 watcher，PID %s", proc.pid)
            return proc.pid
        except Exception as exc:  # noqa: BLE001
            log.error("拉起 watcher 失败: %s", exc)
            return None

    def _remember_spawn(self, pid: Optional[int], now: datetime) -> None:
        if not pid:
            return
        self.state["last_spawn_pid"] = int(pid)
        self.state["last_spawn_at"] = now.isoformat(timespec="seconds")

    def _recent_spawn_alive(self, now: datetime) -> bool:
        """刚拉起的 watcher 可能还没写出 status.json，先给它一个宽限期。"""
        pid = self.state.get("last_spawn_pid")
        if not pid or not pid_alive(pid):
            return False
        raw = self.state.get("last_spawn_at")
        if not raw:
            return False
        try:
            then = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        return (now - then).total_seconds() <= self.spawn_grace_seconds

    def _start_initial(self, now: datetime) -> str:
        """全新安装（没有 status.json）：guardian 负责启动第一份 watcher。"""
        new_pid = self._start_watcher()
        self.state["start_count"] = int(self.state.get("start_count", 0)) + 1
        self.state["last_action"] = "start_initial"
        if new_pid:
            self._remember_spawn(new_pid, now)
        else:
            self.state["consecutive_failures"] = int(
                self.state.get("consecutive_failures", 0)
            ) + 1
        self._save_state()
        append_history(self.history_path, {
            "at": now.isoformat(timespec="seconds"),
            "service": "watcher",
            "action": "start_initial",
            "exit_code": None,
            "exit_reason": "none",
            "new_pid": new_pid,
        })
        log.warning("首次启动 watcher，PID %s", new_pid)
        return ACT_STARTED if new_pid else ACT_RESTART_GONE

    def _start_gallery(self) -> Optional[int]:
        cmd = [
            self.python, "-m", "roomwatch", "gallery",
            "--config", self.config_path,
            "--pid-file", self.gallery_pid_path,
        ]
        if self.dry_run:
            log.info("[dry-run] 本应执行: %s", " ".join(cmd))
            return None
        try:
            log_path = Path(self.gallery_log)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as out:
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
                proc = subprocess.Popen(
                    cmd,
                    cwd=HERE,
                    stdout=out,
                    stderr=out,
                    stdin=subprocess.DEVNULL,
                    creationflags=flags,
                    close_fds=True,
                )
            log.warning("已拉起 gallery，PID %s", proc.pid)
            return proc.pid
        except Exception as exc:  # noqa: BLE001
            log.error("拉起 gallery 失败: %s", exc)
            return None

    def _gallery_pid(self) -> int:
        try:
            return int(
                Path(self.gallery_pid_path).read_text(encoding="utf-8").strip() or 0
            )
        except (OSError, ValueError):
            return 0

    def _gallery_restart_blocked(self, now: datetime) -> Optional[float]:
        failures = int(self.state.get("gallery_failures", 0))
        raw = self.state.get("last_gallery_restart_at")
        if failures <= 0 or not raw:
            return None
        try:
            last = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        wait = backoff_seconds(failures, self.max_backoff)
        elapsed = (now - last).total_seconds()
        return wait - elapsed if elapsed < wait else None

    def _gallery_pid_age(self, now: datetime) -> Optional[float]:
        """PID 文件有多久没更新：相册只在启动成功时写它。

        文件很旧说明上一次确实健康运行过；文件缺失或很新，说明刚拉起的
        相册可能还没来得及监听就退了（端口被保留/占用时就是这样）。
        """
        try:
            mtime = Path(self.gallery_pid_path).stat().st_mtime
        except OSError:
            return None
        return max(0.0, now.timestamp() - mtime)

    def _check_gallery(self, now: datetime) -> str:
        pid = self._gallery_pid()
        action = decide_gallery(pid, pid_alive, cmdline=_psutil_cmdline)
        if action == ACT_OK:
            # 只有「下一次检查时还活着」才算恢复。拉起成功（Popen 返回 PID）
            # 不代表监听成功——端口 bind 失败会立刻退出。
            if int(self.state.get("gallery_failures", 0)):
                log.info("相册已恢复，清空失败计数")
                self.state["gallery_failures"] = 0
                self._save_state()
            return action
        if action != ACT_RESTART_GONE:
            return action
        if self._gallery_restart_blocked(now) is not None:
            return ACT_BACKOFF

        failures = int(self.state.get("gallery_failures", 0))
        spawn_age = None
        raw = self.state.get("last_gallery_spawn_at")
        if raw:
            try:
                spawn_age = (now - datetime.fromisoformat(str(raw))).total_seconds()
            except (TypeError, ValueError):
                spawn_age = None
        recent_spawn = spawn_age is not None and spawn_age < self.crash_window_minutes * 60
        age = self._gallery_pid_age(now)
        # 旧 PID 文件可能一直留在盘上。只有「最近没有拉起过」且文件很旧，
        # 才能认定上一轮活过了整个崩溃窗口；否则会把崩溃循环误判成健康。
        if not recent_spawn and age is not None and age >= self.crash_window_minutes * 60:
            failures = 0
        first_start = not self.state.get("last_gallery_spawn_at")

        new_pid = self._start_gallery()
        if new_pid:
            self.state["last_gallery_spawn_at"] = now.isoformat(timespec="seconds")
            self.state["last_gallery_spawn_pid"] = int(new_pid)
            # 成功拉起 ≠ 健康；除了第一次启动，每次重启都先记一次失败，
            # 等下一次检查看到它还活着再清零。
            if not first_start:
                failures += 1
        else:
            failures += 1
        self.state["gallery_failures"] = failures
        self.state["last_gallery_restart_at"] = now.isoformat(timespec="seconds")
        append_history(self.history_path, {
            "at": now.isoformat(timespec="seconds"),
            "service": "gallery",
            "action": action,
            "exit_code": None,
            "exit_reason": "not-running" if not pid else "unknown",
            "new_pid": new_pid,
            "failures": failures,
        })

        if failures >= self.crash_threshold:
            last_alert = self.state.get("last_gallery_alert_at")
            should_alert = True
            if last_alert:
                try:
                    elapsed = (now - datetime.fromisoformat(str(last_alert))).total_seconds()
                    should_alert = elapsed >= self.crash_window_minutes * 60
                except (TypeError, ValueError):
                    pass
            if should_alert:
                self._notify(crash_alert_text(
                    "gallery", failures, self.crash_window_minutes, None
                ))
                self.state["last_gallery_alert_at"] = now.isoformat(timespec="seconds")
        self._save_state()
        return action

    def _notify(self, text: str) -> None:
        def send() -> None:
            try:
                from .notifier import Notifier

                ok = Notifier(self.cfg.get("notify") or {}).send(text)
                if not ok:
                    log.warning("守护通知未送达：%s", text[:80])
            except Exception as exc:  # noqa: BLE001 - 通知失败不能拖累守护
                log.warning("发送守护通知失败: %s", exc)

        threading.Thread(target=send, daemon=True, name="guardian-notify").start()

    def _check_selfcheck(self, now: datetime) -> None:
        data = health.read_json(self.health_path)
        failures = [
            item for item in ((data or {}).get("selfcheck") or [])
            if isinstance(item, dict) and not item.get("ok", True)
        ]
        if not failures:
            return
        last = self.state.get("last_selfcheck_alert_at")
        if last:
            try:
                if (now - datetime.fromisoformat(str(last))).total_seconds() < 600:
                    return
            except (TypeError, ValueError):
                pass
        detail = "；".join(
            f"{item.get('name', '?')}: {item.get('detail', '')}" for item in failures
        )
        self._notify(f"room-watch：启动自检失败：{detail}")
        self.state["last_selfcheck_alert_at"] = now.isoformat(timespec="seconds")

    def check_once(self) -> str:
        """检查一轮，必要时动手。返回动作字符串。"""
        now = datetime.now()

        if control.read_desired_state(self.root) == "paused":
            if self.state.get("last_desired_state") != "paused":
                self._notify("room-watch：监控已暂停（不会再自动拉起）")
            self.state["last_desired_state"] = "paused"
            self.state["pending_recovery"] = None
            self.state["stall_streak"] = 0
            self.state["last_action"] = "paused"
            self._save_state()
            return ACT_PAUSED

        status = read_status(self.status_path)
        self.state["last_desired_state"] = "running"

        if self._recent_spawn_alive(now) and (
            status is None or not pid_alive(status.get("pid"))
        ):
            self.state["last_action"] = "wait(starting)"
            self._save_state()
            return ACT_WAIT

        if status is None:
            return self._start_initial(now)

        action = decide(
            status,
            now,
            self.stale_seconds,
            int(self.state.get("stall_streak", 0)),
            self.stall_confirmations,
        )

        if action == ACT_WAIT:
            # 状态文件完全缺失时 decide 也给 WAIT，这里再补一步：
            # 如果连进程都找不到（比如从没启动过），就应该拉起
            if status is None:
                self.state["stall_streak"] = int(self.state.get("stall_streak", 0)) + 1
                self.state["last_action"] = "wait(no status)"
                self._save_state()
                log.info("没有状态文件，先观察（第 %s 次）", self.state["stall_streak"])
                return ACT_WAIT
            self.state["stall_streak"] = int(self.state.get("stall_streak", 0)) + 1
            self.state["last_action"] = "wait(stalled)"
            self._save_state()
            log.warning(
                "心跳已停 %s 秒但进程还在，再观察（第 %s/%s 次）",
                int(frame_age_seconds(status, now) or 0),
                self.state["stall_streak"],
                self.stall_confirmations,
            )
            return ACT_WAIT

        if action == ACT_OK:
            if self.state.get("stall_streak"):
                log.info("心跳恢复，清空观察计数")
            self.state["stall_streak"] = 0
            self.state["consecutive_failures"] = 0
            self.state["last_action"] = "ok"
            self._save_state()

            pending = self.state.get("pending_recovery")
            if pending:
                if self.notify_recovery:
                    self._notify(recovery_text(
                        str(pending.get("service") or "watcher"),
                        {"reason": pending.get("reason"), "code": pending.get("code")},
                    ))
                self.state["pending_recovery"] = None
                self._save_state()

            self._check_selfcheck(now)
            if self.supervise_gallery:
                self._check_gallery(now)
            return ACT_OK

        # 需要重启的两种情况，先看退避
        remaining = self._restart_blocked(now)
        if remaining is not None:
            self.state["last_action"] = f"backoff({int(remaining)}s)"
            self._save_state()
            log.warning("重启失败过 %s 次，还需等待 %d 秒",
                        self.state.get("consecutive_failures"), int(remaining))
            return ACT_BACKOFF

        if action == ACT_RESTART_STALLED:
            self._kill((status or {}).get("pid"))
            reason = "进程卡死（心跳停摆）"
        else:
            reason = "进程已不存在"

        status_pid = (status or {}).get("pid")
        exit_record = self._trusted_exit_record(status_pid)
        new_pid = self._start_watcher()
        self._remember_spawn(new_pid, now)
        self.state["restart_count"] = int(self.state.get("restart_count", 0)) + 1
        self.state["stall_streak"] = 0
        self.state["last_restart_at"] = now.isoformat(timespec="seconds")
        self.state["last_action"] = f"{action}: {reason}"
        if new_pid:
            self.state["consecutive_failures"] = 0
        else:
            self.state["consecutive_failures"] = int(self.state.get("consecutive_failures", 0)) + 1
        self._save_state()

        uptime_seconds = None
        started_raw = (status or {}).get("started_at")
        if started_raw:
            try:
                uptime_seconds = round(
                    (now - datetime.fromisoformat(str(started_raw))).total_seconds(), 1
                )
            except (TypeError, ValueError):
                pass
        health_data = health.read_json(self.health_path) or {}
        health_summary = {
            key: health_data.get(key)
            for key in ("loop_seq", "consecutive_errors", "camera_reopens", "rss_mb")
        }

        append_history(self.history_path, {
            "at": now.isoformat(timespec="seconds"),
            "service": "watcher",
            "action": action,
            "exit_code": (exit_record or {}).get("code"),
            "exit_reason": (exit_record or {}).get("reason", "unknown"),
            "uptime_seconds": uptime_seconds,
            "health": health_summary,
        })

        exit_reason = (exit_record or {}).get("reason", "unknown")
        if exit_reason in ("unhandled", "stalled", "camera", "model", "unknown"):
            self.state["pending_recovery"] = {
                "service": "watcher",
                "reason": exit_reason,
                "code": (exit_record or {}).get("code"),
            }
        else:
            self.state["pending_recovery"] = None
        times = [
            str(item) for item in (self.state.get("restart_times") or [])
            if isinstance(item, str)
        ]
        times.append(now.isoformat(timespec="seconds"))
        self.state["restart_times"] = times[-20:]

        count = crash_window_count(times, now, self.crash_window_minutes)
        should_alert = count >= self.crash_threshold
        last_alert = self.state.get("last_crash_alert_at")
        if last_alert:
            try:
                elapsed = (now - datetime.fromisoformat(str(last_alert))).total_seconds()
                if elapsed < self.crash_window_minutes * 60:
                    should_alert = False
            except (TypeError, ValueError):
                pass
        if should_alert:
            self._notify(crash_alert_text(
                "watcher", count, self.crash_window_minutes, exit_record
            ))
            self.state["last_crash_alert_at"] = now.isoformat(timespec="seconds")
        self._save_state()

        log.warning("已重启（%s），累计第 %s 次", reason, self.state["restart_count"])
        return action

    # ---------- 常驻 ----------

    def run_forever(self) -> None:
        log.info("守护启动：每 %d 秒检查一次，卡死判据 %d 秒",
                 self.check_seconds, self.stale_seconds)
        owner = FileLock(
            self.owner_lock_path,
            validate=lambda pid: _cmdline_has(pid, ("guard",)),
        )
        if not owner.acquire():
            log.info("已有 guardian 实例在运行，本实例退出")
            return
        try:
            while True:
                try:
                    with FileLock(self.lock_path) as got:
                        if got:
                            self.check_once()
                        else:
                            log.debug("另一个守护实例正在检查，跳过本轮")
                except KeyboardInterrupt:
                    log.info("收到 Ctrl+C，守卫退出（watcher 不受影响）")
                    return
                except Exception as exc:  # noqa: BLE001 - 守护者自己绝不能崩
                    log.error("检查轮次异常（已吞掉，继续看守）: %s", exc)
                time.sleep(self.check_seconds)
        finally:
            owner.release()


def load_config(path: str) -> dict:
    """主配置 + config.local.yaml 深合并（凭据只在本机覆盖文件里）。"""
    cfg = _merge_config(path)
    cfg["__config_path__"] = os.path.abspath(path)
    return cfg


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="room-watch 守护进程")
    ap.add_argument("--config", default=resolve("config.yaml"))
    ap.add_argument("--once", action="store_true", help="只检查一次就退出（给计划任务用）")
    ap.add_argument("--status", action="store_true", help="打印当前状态与重启历史")
    ap.add_argument("--dry-run", action="store_true", help="只报告该做什么，不真的重启")
    ap.add_argument("--log", help="日志文件路径（用 pythonw 静默启动时必填，否则日志无处可去）")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = []
    if args.log:
        log_path = _abs(args.log)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        handlers.append(logutil.rotating_file_handler(log_path))
    if sys.stderr is not None:  # pythonw 下 stderr 为 None，写 console handler 会抛异常
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers or [logging.NullHandler()],
    )

    cfg_path = _abs(args.config)
    try:
        cfg = load_config(cfg_path)
    except OSError as exc:
        print(f"读不到配置文件 {cfg_path}: {exc}")
        sys.exit(1)

    guard = Guard(cfg, dry_run=args.dry_run)

    if args.status:
        status = read_status(guard.status_path)
        age = frame_age_seconds(status)
        print("=== 守护状态 ===")
        print(f"期望状态     : {control.read_desired_state(guard.root)}")
        gpid = guard._gallery_pid()
        galive = bool(gpid) and pid_alive(gpid)
        print(f"相册         : PID {gpid or '无'}（{'存活' if galive else '未运行'}）")
        print(f"监控状态文件 : {guard.status_path}")
        if status is None:
            print("  内容        : 读不到（监控可能从未启动过）")
        else:
            print(f"  PID         : {status.get('pid')}")
            print(f"  进程存活    : {'是' if pid_alive(status.get('pid')) else '否'}")
            print(f"  最后画面    : {status.get('last_frame_at')}"
                  f"（{int(age) if age is not None else '?'} 秒前）")
        print(f"心跳判据     : 超过 {guard.stale_seconds:.0f} 秒算卡死"
              f"（连续确认 {guard.stall_confirmations} 次）")
        print(f"累计重启     : {guard.state.get('restart_count', 0)} 次")
        print(f"最近动作     : {guard.state.get('last_action')}")
        print(f"最近重启时间 : {guard.state.get('last_restart_at')}")
        print(f"上次退出原因 : {guard.last_exit_reason()}")
        rows = read_history(guard.history_path, limit=5)
        if rows:
            print("最近重启历史 :")
            for row in rows:
                print(f"  {row.get('at')} {row.get('service')} {row.get('action')} "
                      f"{row.get('exit_reason')}")
        else:
            print("最近重启历史 : （暂无）")
        return

    if args.once:
        with FileLock(guard.lock_path) as got:
            if not got:
                log.info("另一个守护实例正在检查，本轮跳过")
                return
            action = guard.check_once()
        log.info("单次检查完成：%s", action)
        return

    guard.run_forever()


if __name__ == "__main__":
    main()
