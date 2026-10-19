"""崩溃钩子：把无法捕获的退出变成可读的记录。

faulthandler 负责原生崩溃（段错误、abort），sys/threading excepthook
负责 Python 未捕获异常，atexit 负责「没写原因就正常退出」的情况。
三者最后都落到 health.write_exit_record 的同一个文件。
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from . import health

log = logging.getLogger("room-watch")

__all__ = ["Diagnostics"]


class Diagnostics:
    def __init__(self, fault_log: str | Path, exit_record: str | Path):
        self.fault_log = Path(fault_log)
        self.exit_record = Path(exit_record)
        self.fault_file = None
        self._installed = False
        self._noted = False
        self._old_sys_hook = None
        self._old_thread_hook = None

    # ------------------------------------------------------------ 安装

    def install(self) -> None:
        if self._installed:
            return
        self.fault_log.parent.mkdir(parents=True, exist_ok=True)
        self.fault_file = open(self.fault_log, "ab", buffering=0)
        faulthandler.enable(file=self.fault_file, all_threads=True)

        self._old_sys_hook = sys.excepthook
        self._old_thread_hook = threading.excepthook
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        atexit.register(self._atexit)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            atexit.unregister(self._atexit)
        except Exception:  # noqa: BLE001
            pass
        if self._old_sys_hook is not None:
            sys.excepthook = self._old_sys_hook
        if self._old_thread_hook is not None:
            threading.excepthook = self._old_thread_hook
        try:
            faulthandler.disable()
        except Exception:  # noqa: BLE001
            pass
        if self.fault_file is not None:
            try:
                self.fault_file.close()
            except OSError:
                pass
            self.fault_file = None
        self._installed = False

    # ------------------------------------------------------------ 记录

    def note_exit(self, code: int, reason: str, *, at: Optional[str] = None) -> None:
        """写退出记录；重复调用以最后一次为准。"""
        try:
            health.write_exit_record(
                self.exit_record,
                pid=os.getpid(),
                code=code,
                reason=reason,
                at=at,
            )
            self._noted = True
        except OSError:
            # 退出路径上写文件失败不能反过来把进程搞挂
            pass

    # ------------------------------------------------------------ 钩子

    def _sys_hook(self, exc_type, exc, tb) -> None:
        try:
            log.error("未捕获异常", exc_info=(exc_type, exc, tb))
        except Exception:  # noqa: BLE001
            pass
        self.note_exit(health.EXIT_UNHANDLED, "unhandled")
        old = self._old_sys_hook
        if old is not None:
            try:
                old(exc_type, exc, tb)
            except Exception:  # noqa: BLE001 - pythonw 下 stderr 为 None
                pass

    def _thread_hook(self, args) -> None:
        name = getattr(getattr(args, "thread", None), "name", "?")
        try:
            log.error(
                "线程 %s 未捕获异常",
                name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:  # noqa: BLE001
            pass
        old = self._old_thread_hook
        if old is not None:
            try:
                old(args)
            except Exception:  # noqa: BLE001
                pass

    def _atexit(self) -> None:
        if not self._noted:
            self.note_exit(health.EXIT_OK, "normal")
