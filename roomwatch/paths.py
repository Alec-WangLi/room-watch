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

# 包目录：<root>/roomwatch（源码与分发包同一个布局）
PACKAGE_DIR = Path(__file__).resolve().parent

# 分发包布局（免安装目录，用户拷走整个文件夹即可）：
#
#   room-watch\                      ← PROJECT_ROOT
#     room-watch.exe                 启动器（tkinter 界面）
#     rw.cmd / 配置.bat / 启动.bat     命令行入口
#     config.yaml                    入库默认配置
#     config.local.yaml              用户配置（向导写这里，不入库）
#     roomwatch\...                  源码
#     models\*.onnx                  权重
#     runtime\python\python.exe      内嵌解释器 + 依赖（客户机不需要装 Python）
#     tools\ffmpeg.exe               转码/合音轨
#     data\ var\                     运行产物
#
# 刻意**不**把源码放进 app\ 子目录：`python -m roomwatch` 依赖
# 「当前目录在 sys.path 里」，多套一层就得额外维护 PYTHONPATH，
# 计划任务/开机自启那种拿不到工作目录的场景会静默失败。


def _layout_root() -> Path:
    """根据包位置推导项目根（不依赖 CWD）。"""
    return PACKAGE_DIR.parent


def _frozen_root() -> Path:
    """打包成 exe 后：从 exe 所在目录往上找第一个含 config.yaml 的目录。

    onefile 模式下 `__file__` 指向临时解包目录（_MEIPASS），
    所以这里必须以 `sys.executable` 为基准，否则数据会写到临时目录里
    然后随进程退出被删掉。
    """
    exe_dir = Path(sys.executable).resolve().parent
    for candidate in (exe_dir, *exe_dir.parents):
        if (candidate / "config.yaml").is_file():
            return candidate
    return exe_dir


def _runtime_root() -> Path:
    explicit = os.environ.get("ROOMWATCH_ROOT")
    if explicit:
        return Path(explicit).resolve()
    if getattr(sys, "frozen", False):
        return _frozen_root()
    return _layout_root()


# 项目根 —— 全项目唯一基准。导入期就定下来，后续所有相对路径都挂在它下面。
PROJECT_ROOT = _runtime_root()

# Inno Setup 卸载项 AppId（与 scripts/room-watch.iss 的 AppId 保持一致）。
UNINSTALL_APP_ID = "{8A3F1C42-6B7D-4E52-9C1A-7D3E5F2B8A61}_is1"
UNINSTALL_KEY = ("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
                 + UNINSTALL_APP_ID)

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
DIAG_DIR = VAR_DIR / "diag"


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

# 分发包内的相对位置（与是否 frozen 无关，统一挂在项目根下）
RUNTIME_DIR = PROJECT_ROOT / "runtime"
TOOLS_DIR = PROJECT_ROOT / "tools"

# 内嵌解释器可能的落点：Windows 是 python.exe，Linux/macOS 是 bin/python3
_BUNDLED_CANDIDATES = (
    Path("runtime") / "python" / "python.exe",
    Path("runtime") / "python" / "bin" / "python3",
    Path("runtime") / "python" / "bin" / "python",
)

FFMPEG_CANDIDATES = (
    Path("tools") / "ffmpeg.exe",
    Path("tools") / "ffmpeg",
)


def bundled_python() -> Optional[str]:
    """分发包内嵌的解释器路径；源码环境下返回 None。

    这是「客户机不用装 Python」的关键：整个 runtime/python 目录随包分发。
    """
    for rel in _BUNDLED_CANDIDATES:
        candidate = PROJECT_ROOT / rel
        if candidate.is_file():
            return str(candidate)
    return None


def is_portable() -> bool:
    """当前是不是在免安装分发包里跑（有内嵌解释器）。"""
    return bundled_python() is not None


def ffmpeg_exe() -> Optional[str]:
    """分发包内自带的 ffmpeg；没有则 None（回落到 PATH）。"""
    for rel in FFMPEG_CANDIDATES:
        candidate = PROJECT_ROOT / rel
        if candidate.is_file():
            return str(candidate)
    return None


def find_python() -> str:
    """挑一个能跑本项目的解释器。

    顺序：ROOMWATCH_PYTHON → 内嵌解释器 →（打包成 exe 时）已知可用路径
          → 当前进程解释器 → 已知可用路径 → "python"。

    内嵌解释器排在 frozen 分支之前，是因为分发包里**必须**用它：
    客户机上通常没有第二个装了 cv2 的 Python，落到已知路径只会失败。

    打包后的 exe 里 `sys.executable` 是 exe 自己，不能拿来跑监控，
    所以 frozen 时必须先看内嵌/已知路径。
    """
    explicit = os.environ.get("ROOMWATCH_PYTHON")
    if explicit and Path(explicit).exists():
        return explicit

    bundled = bundled_python()
    if bundled:
        return bundled

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

    与导入期的 `PROJECT_ROOT` 同源，只是每次调用重新求值，
    方便测试注入 `ROOMWATCH_ROOT` 或在同一进程里临时切换目录。
    """
    return _runtime_root()


def has_module(name: str, python: Optional[str] = None) -> bool:
    """检查某个解释器能否 import 指定模块（给 `rw doctor` 用）。"""
    import subprocess

    from . import procutil

    exe = python or find_python()
    try:
        proc = subprocess.run(
            [exe, "-c", f"import {name}"],
            capture_output=True,
            timeout=20,
            creationflags=procutil.no_window_flags(),
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False
