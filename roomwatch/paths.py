"""项目路径与解释器解析 —— 全项目唯一的「相对路径基准」。

为什么要有这个模块
------------------
包化之前，`faces.py` / `identity.py` 用 `dirname(__file__)` 拼 `models/`，
`guardian.py` 用 `HERE` 拼 `watcher.py`，而 `config.yaml` 里的
`captures` / `events.db` / `identity/owner.npz` 又是**相对当前工作目录**的。
三种基准混在一起，结果就是「换个目录启动就找不到自己的数据」，
而且从源码挪动任何一个文件都会静默失效（模型找不到时 detector 只是不工作，不报错）。

现在统一成一条规则：

- `PROJECT_ROOT` = 包目录的上一级，由本文件位置推导，**与 CWD 无关**；
- 配置里的相对路径一律经 `resolve()` 落到 `PROJECT_ROOT` 下；
- 绝对路径原样返回（测试注入临时目录时依赖这一点）。

解释器解析
----------
本机有四个 Python，只有 `envs/default` 那个装了 cv2。`find_python()` 按
「显式指定 → 当前解释器 → 已知可用路径」的顺序挑，避免用户踩
`ModuleNotFoundError: No module named 'cv2'`（见 AGENTS.md D.10）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# 包目录：<root>/roomwatch；项目根：<root>
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

# 运行时目录约定（全部不入库，见 .gitignore）
DATA_DIR = PROJECT_ROOT / "data"
VAR_DIR = PROJECT_ROOT / "var"
CAPTURES_DIR = DATA_DIR / "captures"
IDENTITY_DIR = DATA_DIR / "identity"
LOG_DIR = VAR_DIR / "logs"
STATE_DIR = VAR_DIR / "state"
DB_DIR = VAR_DIR / "db"
MEDIA_DIR = VAR_DIR / "media"
CACHE_DIR = VAR_DIR / "cache"


def resolve(path: str | os.PathLike[str] | None, default: str = "") -> str:
    """相对路径 → 项目根下的绝对路径；绝对路径原样返回。

    `None` / 空串返回 `default`（同样按相对路径解析），
    方便 `resolve(cfg.get("dir"), "data/captures")` 这种写法。
    """
    raw = str(path) if path not in (None, "") else str(default)
    if not raw:
        return ""
    p = Path(raw)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def ensure_dir(path: str | os.PathLike[str]) -> str:
    """建目录并返回其绝对路径（幂等）。"""
    resolved = resolve(path)
    os.makedirs(resolved, exist_ok=True)
    return resolved


# 已知能跑本项目的解释器（按优先级），最后才退回 PATH 上的 python
_KNOWN_PYTHONS = (
    Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "Scripts" / "python.exe",
)


def find_python() -> str:
    """挑一个能跑本项目的解释器。

    顺序：ROOMWATCH_PYTHON →（打包成 exe 时）已知可用路径 → 当前进程解释器
          → 已知可用路径 → "python"。
    打包后的 exe 里 `sys.executable` 是 exe 自己，不能拿来跑监控，
    所以 frozen 时必须先看已知路径。
    """
    explicit = os.environ.get("ROOMWATCH_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        for candidate in _KNOWN_PYTHONS:
            if candidate.exists():
                return str(candidate)

    current = sys.executable
    if current and Path(current).exists() and not frozen:
        return current

    for candidate in _KNOWN_PYTHONS:
        if candidate.exists():
            return str(candidate)

    return "python"


def runtime_root() -> Path:
    """界面（可能是打包后的 exe）该把哪个目录当作项目根。

    - `ROOMWATCH_ROOT` 显式指定优先；
    - 打包后：从 exe 所在目录向上找第一个含 `config.yaml` 的目录
      （exe 通常放在 dist/，项目根在上一级）；
    - 源码运行：就是 `PROJECT_ROOT`。
    """
    explicit = os.environ.get("ROOMWATCH_ROOT")
    if explicit:
        return Path(explicit).resolve()

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir, *exe_dir.parents):
            if (candidate / "config.yaml").is_file():
                return candidate
        return exe_dir

    return PROJECT_ROOT


def has_module(name: str, python: Optional[str] = None) -> bool:
    """检查某个解释器能否 import 指定模块（给 `rw doctor` 用）。"""
    import subprocess

    exe = python or find_python()
    try:
        proc = subprocess.run(
            [exe, "-c", f"import {name}"],
            capture_output=True,
            timeout=20,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False
