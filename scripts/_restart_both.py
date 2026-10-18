"""巡检用：确认 watcher / gallery 已死后，以分离进程方式重新拉起。

不应在二者仍存活时调用。启动参数与 AGENTS.md 记录一致：
  watcher: -m roomwatch.watcher --config config.yaml --log var/logs/watcher.log
  gallery: -m roomwatch.gallery --port 8765

用当前解释器（sys.executable）拉起，不写死任何本机路径。
分离进程的 creationflags 是 Windows 专有，其他平台退化为普通 Popen。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
FLAGS = 0
if sys.platform == "win32":
    FLAGS = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def launch(args):
    return subprocess.Popen(
        [PY, *args],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=FLAGS,
    ).pid


w = launch(["-m", "roomwatch.watcher", "--config", "config.yaml",
            "--log", "var/logs/watcher.log"])
time.sleep(2)
g = launch(["-m", "roomwatch.gallery", "--port", "8765"])
print(f"watcher_pid={w} gallery_pid={g}")
