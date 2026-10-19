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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from . import health
from . import logutil
from .paths import PROJECT_ROOT, resolve

log = logging.getLogger("guardian")

# decide() 的返回值
ACT_OK = "ok"                    # 一切正常
ACT_WAIT = "wait"                # 疑似卡死，再观察一轮
ACT_RESTART_GONE = "restart_gone"      # 进程没了
ACT_RESTART_STALLED = "restart_stalled"  # 进程在但心跳停了
ACT_BACKOFF = "backoff"          # 在退避窗口内，先不动

HERE = str(PROJECT_ROOT)


def _abs(path: str) -> str:
    return resolve(path)


def pid_alive(pid: Optional[int]) -> bool:
    """PID 是否对应一个活着的进程。"""
    if not pid or pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
    except Exception:  # noqa: BLE001 - psutil 缺失或进程刚死都可能抛
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:  # noqa: BLE001
            return False


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


class FileLock:
    """极简的单实例锁：拿不到就说明别人在干活，本轮跳过。

    锁文件里写的是 PID，所以「上个进程崩了留下陈旧锁」能被识别并清理，
    否则守护者会永久失效——这是锁文件最常见的坑。
    """

    def __init__(self, path: str):
        self.path = path
        self.acquired = False

    def _stale(self) -> bool:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return True  # 读不出内容，当陈旧处理
        return not pid_alive(pid)

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
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            log.warning("写守护状态失败: %s", exc)

    def last_exit_reason(self) -> str:
        """上一次 watcher 退出原因；记录缺失或损坏都按 unknown 处理。"""
        record = health.read_exit_record(self.exit_record_path)
        if not record:
            return "unknown"
        return str(record.get("reason") or "unknown")

    # ---------- 动作 ----------

    def _restart_blocked(self, now: datetime) -> Optional[float]:
        """还在退避窗口内吗？返回剩余秒数。"""
        last = self.state.get("last_restart_at")
        if not last:
            return None
        try:
            last_dt = datetime.fromisoformat(str(last))
        except (TypeError, ValueError):
            return None
        wait = backoff_seconds(int(self.state.get("consecutive_failures", 0)), self.max_backoff)
        elapsed = (now - last_dt).total_seconds()
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

    def _start_watcher(self) -> Optional[int]:
        """拉起 watcher。返回新 PID（拿不到就 None）。"""
        cmd = [
            self.python,
            "-m",
            "roomwatch",
            "start",
            "--config",
            self.config_path,
            "--log",
            self.watcher_log,
        ]
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

    def check_once(self) -> str:
        """检查一轮，必要时动手。返回动作字符串。"""
        now = datetime.now()
        status = read_status(self.status_path)

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

        exit_record = health.read_exit_record(self.exit_record_path)
        new_pid = self._start_watcher()
        self.state["restart_count"] = int(self.state.get("restart_count", 0)) + 1
        self.state["stall_streak"] = 0
        self.state["last_restart_at"] = now.isoformat(timespec="seconds")
        self.state["last_action"] = f"{action}: {reason}"
        if new_pid:
            self.state["consecutive_failures"] = 0
        else:
            self.state["consecutive_failures"] = int(self.state.get("consecutive_failures", 0)) + 1
        self._save_state()

        append_history(self.history_path, {
            "at": now.isoformat(timespec="seconds"),
            "service": "watcher",
            "action": action,
            "exit_code": (exit_record or {}).get("code"),
            "exit_reason": (exit_record or {}).get("reason", "unknown"),
        })

        log.warning("已重启（%s），累计第 %s 次", reason, self.state["restart_count"])
        return action

    # ---------- 常驻 ----------

    def run_forever(self) -> None:
        log.info("守护启动：每 %d 秒检查一次，卡死判据 %d 秒",
                 self.check_seconds, self.stale_seconds)
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


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
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
