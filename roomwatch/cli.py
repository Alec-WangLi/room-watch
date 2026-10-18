"""全局命令 `rw` —— 项目唯一的命令行入口。

为什么要它
----------
包化前是「13 个模块各有 argparse，用户要记住 13 套写法」，还要记住本机
四个 Python 里只有 `envs/default` 有 cv2（踩错就报 No module named 'cv2'）。
现在统一成一张命令表：

    rw start / dry-run / probe / status     监控
    rw gallery                              本地相册
    rw guard [--once|--status]              守护进程
    rw enroll / whoami                      人脸注册与验证
    rw say / ab / volume-check / volume-set 语音与音量
    rw storage                              磁盘占用
    rw push / push-video PATH              推送配置 / 验证录像推送
    rw test [--fast] / doctor / version     工程自检
    rw gui                                  桌面界面

设计约束
--------
- **惰性导入**：`rw version` / `rw doctor` 不该为了打印一行版本去 import cv2。
  各子命令的实现模块只在真正执行时才导入，这样界面进程（可能没有 cv2）
  也能安全使用本模块。
- **单一真相**：子命令不重新定义参数，只做「命令名 → 原模块 argv」的翻译，
  原模块的 argparse 继续负责校验与帮助文本。
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from . import paths

# 让 tests 能 `cli.__version__`
__all__ = [
    "COMMANDS", "Command", "Check", "UsageError", "main", "translate",
    "build_parser", "run_checks", "test_files",
]


class UsageError(Exception):
    """命令用法错误（参数缺失 / 不认识的子命令），由 main 转成退出码 2。"""


@dataclass(frozen=True)
class Command:
    """一条全局命令的元信息与参数翻译规则。

    module   : 实现它的模块名（"" 表示由本模块内部处理）
    prefix   : 固定前置参数，如 status → ("--status",)
    takes    : 需要从用户输入里取一个值再拼进 prefix 的占位符位置
               例如 volume-set 的 "--volume-set"，whoami 的 "--whoami"
    """

    summary: str
    module: str = ""
    prefix: tuple[str, ...] = ()
    takes: Optional[str] = None
    requires_value: bool = False


# 命令表就是规范本身：新增功能先在这里登记，否则 `rw --help` 里看不到。
COMMANDS: dict[str, Command] = {
    "start": Command("启动监控（前台常驻，Ctrl+C 退出）", "watcher"),
    "dry-run": Command("试跑：抓拍存档但不推送，用来验证误报率", "watcher", ("--dry-run",)),
    "probe": Command("拍一张预览图后退出，用来确认摄像头角度", "watcher", ("--probe",)),
    "status": Command("看监控是否在线（重算心跳，不信文件里的 stale 字段）", "watcher", ("--status",)),
    "enroll": Command("注册本人人脸：--from IMG 用已有照片，不给则现场采集", "watcher"),
    "whoami": Command("判断某张图里的人是不是本人：rw whoami IMG", "watcher",
                      takes="--whoami", requires_value=True),
    "say": Command("试听响度处理后的提示音", "watcher", ("--say",)),
    "say-raw": Command("试听未处理的原始提示音", "watcher", ("--say-raw",)),
    "ab": Command("A/B 对比：先原始再处理", "watcher", ("--ab",)),
    "volume-check": Command("诊断系统音量与默认输出设备", "watcher", ("--volume-check",)),
    "volume-set": Command("直接设置系统主音量（0~100）", "watcher",
                          takes="--volume-set", requires_value=True),
    "storage": Command("查看磁盘占用、容量上限与最旧文件", "watcher", ("--storage",)),
    "gallery": Command("启动本地相册（默认 127.0.0.1:8765）", "gallery"),
    "guard": Command("守护进程：保证监控进程一直活着", "guardian"),
    "push": Command("配置微信/Server酱推送渠道", "setup_push"),
    "push-video": Command("把一段录像发到企业微信（验证视频推送）：rw push-video PATH",
                          "videopush", prefix=("--video",), takes="--video",
                          requires_value=True),
    "test": Command("跑全部测试（--fast 跳过最慢的集成测试）"),
    "doctor": Command("环境自检：解释器、依赖、模型、目录、端口、钩子"),
    "version": Command("打印版本号"),
    "gui": Command("打开桌面界面"),
}

# 这些子命令需要把用户输入原样交给实现模块
_PASSTHROUGH_MODULES = {"gallery", "guardian", "setup_push"}


def translate(name: str, argv: Sequence[str]) -> list[str]:
    """把 `rw <name> <argv>` 翻译成实现模块能吃的 argv。

    只做机械翻译，不校验取值合法性 —— 那是原模块 argparse 的职责。
    但「该给的值没给」属于用法错误，这里直接抛 UsageError，
    否则会把 "rw whoami" 变成 "--whoami None" 这种更难懂的错误。
    """
    if name not in COMMANDS:
        raise UsageError(f"未知命令：{name}")

    cmd = COMMANDS[name]
    rest = list(argv)

    if name == "enroll":
        if rest and rest[0] in ("--from", "-f"):
            if len(rest) < 2:
                raise UsageError("rw enroll --from 后面要跟一张图片路径")
            return ["--enroll-from", rest[1], *rest[2:]]
        return ["--enroll", *rest]

    if cmd.requires_value:
        if not rest:
            raise UsageError(f"rw {name} 需要一个值")
        value, tail = rest[0], rest[1:]
        if value.startswith("-"):
            raise UsageError(f"rw {name} 需要一个值，而不是选项 {value}")
        flag = cmd.prefix or ((cmd.takes,) if cmd.takes else ())
        return [*flag, value, *tail]

    if cmd.takes:  # 目前只有 whoami/volume-set，上面已处理
        if not rest:
            raise UsageError(f"rw {name} 需要一个值")
        return [cmd.takes, rest[0], *rest[1:]]

    return [*cmd.prefix, *rest]


def build_parser() -> argparse.ArgumentParser:
    """顶层解析器。子命令用 REMAINDER 原样收集，帮助文本由各模块自己提供。"""
    parser = argparse.ArgumentParser(
        prog="rw",
        description="room-watch 全局命令。每个子命令都支持追加原模块参数（--config 等）。",
        epilog="例：rw start --config config.local.yaml / rw gallery --port 9000",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"room-watch {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    for name, spec in COMMANDS.items():
        p = sub.add_parser(name, help=spec.summary, description=spec.summary, add_help=False)
        p.add_argument("args", nargs=argparse.REMAINDER, help="透传给原模块的参数")
    return parser


# ---------------------------------------------------------------- 子命令实现


def _run_module(module_name: str, argv: list[str]) -> int:
    module = importlib.import_module(f".{module_name}", package="roomwatch")
    module.main(argv)
    return 0


def test_files(fast: bool = False) -> list[str]:
    """返回要跑的测试文件名（按名字排序，排除 __init__ 之类）。"""
    tests_dir = paths.PROJECT_ROOT / "tests"
    names = sorted(p.name for p in tests_dir.glob("test_*.py"))
    if fast:
        names = [n for n in names if n != "test_integration.py"]
    return names


def _cmd_test(argv: Sequence[str]) -> int:
    fast = "--fast" in argv
    others = [a for a in argv if a != "--fast"]
    if others:
        raise UsageError(f"rw test 只接受 --fast，收到：{' '.join(others)}")

    files = test_files(fast=fast)
    print(f"运行 {len(files)} 个测试文件" + ("（--fast 已跳过集成测试）" if fast else ""))
    failed: list[str] = []
    for name in files:
        print(f"\n=== {name} ===", flush=True)
        proc = subprocess.run(
            [sys.executable, str(paths.PROJECT_ROOT / "tests" / name)],
            cwd=str(paths.PROJECT_ROOT),
        )
        if proc.returncode != 0:
            failed.append(name)

    print("\n---- 汇总 ----")
    if failed:
        print(f"未通过 {len(failed)} 个：{', '.join(failed)}")
        return 1
    print("全部通过")
    return 0


@dataclass
class Check:
    """doctor 的一条检查结果。detail 要写「怎么修」，而不只是「错了」。"""

    name: str
    ok: bool
    detail: str
    fatal: bool = True


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 10)
    return Check("解释器", ok, f"{sys.executable} · Python {sys.version.split()[0]}",
                 fatal=not ok)


def _check_config(root: Path) -> Check:
    cfg = root / "config.yaml"
    return Check("配置文件", cfg.is_file(), str(cfg) if cfg.is_file() else f"缺失：{cfg}")


def _check_deps() -> Check:
    import importlib.util

    required = ("cv2", "numpy", "yaml")
    optional = ("ultralytics", "pyttsx3", "sounddevice", "psutil")
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    missing_opt = [m for m in optional if importlib.util.find_spec(m) is None]
    detail = "必需依赖齐全" if not missing else f"缺：{', '.join(missing)}（用 rw doctor 提示的解释器装）"
    if missing_opt:
        detail += f"；可选未装：{', '.join(missing_opt)}"
    return Check("依赖", not missing, detail, fatal=bool(missing))


def _check_models(root: Path) -> Check:
    # yolov8n.pt 不再是必需项：运行时默认走 ONNX，torch 只在「没有 ONNX」或
    # 需要重新导出时才用。分发包里不带 torch 也能完整运行。
    expected = [
        root / "models" / "yolov8n.onnx",
        root / "models" / "face_detection_yunet_2023mar.onnx",
        root / "models" / "face_recognition_sface_2021dec.onnx",
    ]
    missing = [p.name for p in expected if not p.is_file()]
    detail = "权重齐全（人体检测走 ONNX，不需要 torch）" if not missing else f"缺：{', '.join(missing)}"
    if not missing and not (root / "yolov8n.pt").is_file():
        detail += "；未带 yolov8n.pt（仅重新导出 ONNX 时需要）"
    return Check("模型", not missing, detail)


def _check_dirs(root: Path) -> Check:
    targets = [root / "data" / "captures", root / "var" / "logs", root / "var" / "state",
               root / "var" / "db", root / "var" / "media"]
    bad: list[str] = []
    for d in targets:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".rw-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{d.name}({exc.__class__.__name__})")
    return Check("目录", not bad, "读写正常" if not bad else f"不可写：{', '.join(bad)}")


def _check_owner(root: Path) -> Check:
    owner = root / "data" / "identity" / "owner.npz"
    if owner.is_file():
        return Check("人脸模板", True, str(owner))
    return Check("人脸模板", True, "未注册（rw enroll 可注册；未注册时所有脸按访客处理）",
                 fatal=False)


def _check_hooks(root: Path) -> Check:
    try:
        proc = subprocess.run(
            ["git", "config", "core.hooksPath"], cwd=str(root),
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("提交钩子", False, f"无法执行 git：{exc}", fatal=False)
    value = proc.stdout.strip()
    if value == ".githooks":
        return Check("提交钩子", True, "core.hooksPath = .githooks")
    return Check("提交钩子", False,
                 "未启用：跑 scripts/setup-hooks.bat（否则真实凭据可能进历史）", fatal=False)


def _check_port(port: int = 8765) -> Check:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return Check("相册端口", True, f"127.0.0.1:{port} 空闲", fatal=False)
    except OSError:
        return Check("相册端口", False, f"127.0.0.1:{port} 已被占用（相册可能已在跑）", fatal=False)
    finally:
        sock.close()


def run_checks(project_root: Optional[Path] = None) -> list[Check]:
    """跑全部自检项。返回列表而不是直接打印，方便单测。"""
    root = Path(project_root) if project_root else paths.PROJECT_ROOT
    return [
        _check_python(),
        _check_config(root),
        _check_deps(),
        _check_models(root),
        _check_dirs(root),
        _check_owner(root),
        _check_hooks(root),
        _check_port(),
    ]


def _cmd_doctor(argv: Sequence[str]) -> int:
    if argv:
        raise UsageError(f"rw doctor 不接受参数，收到：{' '.join(argv)}")

    print(f"room-watch {__version__} 环境自检\n")
    checks = run_checks()
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.fatal else "WARN")
        print(f"  [{mark}] {c.name.ljust(width)}  {c.detail}")

    fatal = [c for c in checks if not c.ok and c.fatal]
    print()
    if fatal:
        print(f"{len(fatal)} 项不通过：{', '.join(c.name for c in fatal)}")
        return 1
    print("核心检查全部通过。")
    return 0


def _cmd_version(argv: Sequence[str]) -> int:
    if argv:
        raise UsageError(f"rw version 不接受参数，收到：{' '.join(argv)}")
    sha = ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(paths.PROJECT_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    suffix = f" (git {sha})" if sha else ""
    print(f"room-watch {__version__}{suffix}")
    return 0


def _cmd_gui(argv: Sequence[str]) -> int:
    from . import gui

    return gui.main(list(argv))


# ---------------------------------------------------------------- 入口


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0
    if argv[0] == "--version":
        return _cmd_version([])

    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"未知命令：{name}\n用 rw --help 查看全部命令", file=sys.stderr)
        return 2

    try:
        if name == "test":
            return _cmd_test(rest)
        if name == "doctor":
            return _cmd_doctor(rest)
        if name == "version":
            return _cmd_version(rest)
        if name == "gui":
            return _cmd_gui(rest)

        spec = COMMANDS[name]
        translated = translate(name, rest)
        return _run_module(spec.module, translated)
    except UsageError as exc:
        print(f"{exc}\n用 rw --help 查看全部命令", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
