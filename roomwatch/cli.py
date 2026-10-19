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
    rw setup                               首次配置向导（摄像头/推送/自启）
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
import json
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
    "start": Command("启动监控（后台常驻，由 guardian 托管；--foreground 前台运行）"),
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
    "stop": Command("优雅停止监控：先让录像收尾，再退出"),
    "gallery": Command("启动本地相册（默认 127.0.0.1:8765）", "gallery"),
    "config": Command("读取或修改界面设置（--json / --set 键=值 / --test）"),
    "autostart": Command("开机自启：查看状态或开启/关闭（--json / --enable / --disable）"),
    "identity": Command("查看或清除本人人脸模板（--json / --clear）"),
    "guard": Command("守护进程：保证监控进程一直活着", "guardian"),
    "push": Command("配置微信/Server酱推送渠道", "setup_push"),
    "setup": Command("首次配置向导：摄像头、微信推送、相册密码、开机自启", "wizard"),
    "push-video": Command("把一段录像发到企业微信（验证视频推送）：rw push-video PATH",
                          "videopush", prefix=("--video",), takes="--video",
                          requires_value=True),
    "test": Command("跑全部测试（--fast 跳过最慢的集成测试）"),
    "doctor": Command("环境自检：解释器、依赖、模型、目录、端口、钩子"),
    "diag": Command("生成诊断包（日志/脱敏配置/健康快照）"),
    "update": Command("检查并安装更新（--check 只查，--yes 允许跨次版本）"),
    "version": Command("打印版本号"),
    "gui": Command("打开桌面界面"),
}

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
    # 分发包（安装包装出来的目录）没有 .git，提交钩子这项本来就无意义；
    # 之前一律报 WARN，客户以为装坏了。
    if not (root / ".git").exists():
        return Check("提交钩子", True, "非源码目录，跳过（分发包无需此检查）", fatal=False)
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
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10013:
            return Check(
                "相册端口", False,
                f"127.0.0.1:{port} 被 Windows 保留（端口排除范围）；"
                "相册启动时会自动改用其它端口，不需要手工处理",
                fatal=False,
            )
        return Check("相册端口", False,
                     f"127.0.0.1:{port} 已被占用（相册可能已在跑）", fatal=False)
    finally:
        sock.close()


def _check_runtime() -> Check:
    """分发包里必须走内嵌解释器；源码环境下这项只是提示。"""
    from .paths import bundled_python

    exe = bundled_python()
    if exe:
        return Check("内嵌运行时", True, f"{exe}（客户机无需安装 Python）", fatal=False)
    return Check("内嵌运行时", True, "源码环境：使用本机 Python（分发包才需要 runtime/python）",
                 fatal=False)


def _check_ffmpeg() -> Check:
    """ffmpeg 缺席不是致命错误：录像照录，只是没有音轨、视频推送发不出去。"""
    import shutil

    from .paths import ffmpeg_exe

    found = ffmpeg_exe() or shutil.which("ffmpeg")
    if found:
        return Check("ffmpeg", True, str(found), fatal=False)
    return Check("ffmpeg", False,
                 "没找到：录像会缺音轨、企业微信发视频会失败（纯图片推送不受影响）",
                 fatal=False)


def run_checks(project_root: Optional[Path] = None) -> list[Check]:
    """跑全部自检项。返回列表而不是直接打印，方便单测。"""
    root = Path(project_root) if project_root else paths.PROJECT_ROOT
    return [
        _check_python(),
        _check_runtime(),
        _check_config(root),
        _check_deps(),
        _check_models(root),
        _check_ffmpeg(),
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


def _cmd_update(argv: Sequence[str]) -> int:
    """检查 / 安装更新。

    退出码：0 已是最新或已完成；1 失败；10 有更新但需要确认（脚本可据此判断）。

    `--json` 是给界面用的：只输出**一行** JSON，过程信息一律不打印，
    并且隐含 `--check`（界面拿到结果后自己决定要不要更新）。
    """
    from datetime import datetime

    from . import updater
    from .config import load_config

    if "--help" in argv or "-h" in argv:
        print(
            "用法：rw update [--check] [--yes] [--force] [--no-restart]\n"
            "                [--json] [--wait-exit PID] [--relaunch-gui]\n"
            "  --check        只检查，不下载不安装\n"
            "  --yes          允许跨次版本/主版本升级（版本纪律要求显式确认）\n"
            "  --force        允许降级（一般只用于回滚到旧版排障）\n"
            "  --no-restart   装完不自动把监控拉起来\n"
            "  --json         机器可读输出，隐含 --check（界面用）\n"
            "  --wait-exit    先等这个进程号退出再安装（界面自己触发更新时用）\n"
            "  --relaunch-gui 装完自动重新打开界面"
        )
        return 0

    flags = {"--check", "--yes", "--force", "--no-restart", "--json", "--relaunch-gui"}
    json_mode = "--json" in argv
    check_only = "--check" in argv or json_mode
    assume_yes = "--yes" in argv
    force = "--force" in argv
    do_restart = "--no-restart" not in argv
    relaunch_gui = "--relaunch-gui" in argv

    wait_pid = 0
    unknown: list[str] = []
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if item == "--wait-exit":
            if idx + 1 >= len(argv) or not argv[idx + 1].isdigit():
                raise UsageError("--wait-exit 后面要跟一个进程号（数字）")
            wait_pid = int(argv[idx + 1])
            idx += 2
            continue
        if item not in flags:
            unknown.append(item)
        idx += 1
    if unknown:
        raise UsageError(f"rw update 不认识这些参数：{' '.join(unknown)}")

    cfg_path = paths.PROJECT_ROOT / "config.yaml"
    cfg = load_config(str(cfg_path)) if cfg_path.is_file() else {}
    opt = updater.update_config(cfg)

    current = updater.Version.parse(__version__)
    payload: dict = {
        "current": str(current),
        "latest": "",
        "policy": "",
        "tag": "",
        "notes": "",
        "error": "",
        "checked_at": "",
        "check_interval_hours": opt["check_interval_hours"],
        "installed": False,
        "handed_off": False,
    }

    def say(*parts: object) -> None:
        """`--json` 模式下静默，保证 stdout 只有那一行 JSON。"""
        if not json_mode:
            print(*parts)

    def done(code: int, **fields: object) -> int:
        payload.update(fields)
        if payload["latest"]:
            if not payload["checked_at"]:
                payload["checked_at"] = datetime.now().isoformat(timespec="seconds")
            # 检查结果落盘，界面下次启动按 check_interval_hours 复用，不用每次联网
            updater.save_check_cache({key: payload[key] for key in (
                "checked_at", "current", "latest", "policy", "tag", "notes",
                "check_interval_hours")})
        if json_mode:
            print(json.dumps(payload, ensure_ascii=False))
        return code

    say(f"本地版本  {current}")
    if not opt["enabled"]:
        say("自动更新已关闭（update.enabled=false），跳过。")
        return done(0, policy="disabled")

    say(f"检查仓库  {opt['repo']}")
    try:
        release = updater.fetch_latest(opt["repo"])
    except updater.UpdateError as exc:
        say(f"✗ {exc}")
        return done(1, error=str(exc))

    latest = release.version
    when = (release.published_at or "")[:10]
    say(f"远端版本  {latest}" + (f"（发布 {when}）" if when else ""))

    policy = updater.decide(current, latest,
                            allow_downgrade=force or opt["allow_downgrade"])
    found = {"latest": str(latest), "policy": policy, "tag": release.tag,
             "notes": (release.notes or "").strip()}

    if policy == "up-to-date":
        say("\n已是最新版本。")
        return done(0, **found)
    if policy == "blocked-downgrade":
        say(f"\n远端 {latest} 比本地 {current} 旧，默认不降级。"
            "确认要回滚请加 --force。")
        return done(0, **found)
    if policy == "downgrade":
        say(f"\n注意：这是降级（{current} → {latest}），因为给了 --force 才继续。")

    if policy in ("confirm-minor", "confirm-major"):
        kind = "次版本" if policy == "confirm-minor" else "主版本"
        say(f"\n这是一个{kind}升级（{current} → {latest}）。")
        say("按项目版本纪律，跨次版本要显式确认，不会自动安装。")
        if not (assume_yes or opt["allow_minor"]):
            say("确认升级请重跑：rw update --yes")
            return done(10, **found)

    if release.notes.strip():
        say("\n更新说明：")
        for line in release.notes.strip().splitlines()[:20]:
            say("  " + line)

    if check_only:
        say("\n有新版本可用（--check 模式，未安装）。")
        return done(10, **found)

    # 界面自己点「立即更新」时，界面进程还占着 room-watch.exe，
    # 安装器换不掉正在运行的可执行文件 —— 先等它退出。
    if wait_pid and not updater.wait_pid_exit(wait_pid, timeout=30.0):
        say(f"✗ 进程 {wait_pid} 在 30 秒内没有退出，已取消安装。")
        return done(1, error="等待界面退出超时", **found)

    asset = release.installer_asset()
    if not asset:
        say(f"\n✗ release {release.tag} 里没有 room-watch-<版本>-setup.exe，无法自动更新。")
        return done(1, error="release 里没有安装包资产", **found)

    expected = updater.expected_sha256(release)
    if not expected:
        # release 说明里没写摘要时，再看有没有 <安装包>.sha256 这个资产
        expected = updater.fetch_sha256_asset(release)
    if not expected and opt["require_checksum"]:
        say("\n✗ 该 release 没有提供 SHA256（*.sha256 资产或说明里的 sha256:），")
        say("  为安全起见拒绝安装。可临时在配置里设 update.require_checksum=false。")
        return done(1, error="缺少 SHA256 摘要", **found)

    dest = updater.cache_dir() / asset["name"]
    # 先清掉上次留下的安装包（每个 142MB），避免 var/cache/updates 越堆越大
    updater.prune_downloads(keep=dest)
    size_mb = int(asset.get("size") or 0) / 1048576
    # 走 302 回退时拿不到资产列表，size 是 0 —— 别显示成「0.0 MB」
    say(f"\n下载 {asset['name']}" + (f"（{size_mb:.1f} MB）" if size_mb else "") + "…")

    def _bar(step: int, total: int) -> None:
        if total and not json_mode:
            pct = step * 100 // total
            print(f"\r  {pct:3d}%  {step / 1048576:.1f}/{total / 1048576:.1f} MB", end="", flush=True)

    try:
        updater.download(asset["browser_download_url"], dest, progress=_bar)
    except updater.UpdateError as exc:
        say(f"\n✗ {exc}")
        return done(1, error=str(exc), **found)
    say()

    if expected:
        if not updater.verify_sha256(dest, expected):
            dest.unlink(missing_ok=True)
            say("✗ SHA256 校验失败，已删除下载文件。")
            say(f"  期望 {expected}")
            return done(1, error="SHA256 校验失败", **found)
        say("✓ SHA256 校验通过")
    else:
        say("⚠ 未提供 SHA256，跳过校验（update.require_checksum=false）")

    # 不能只停 watcher：相册也跑在包内 runtime 上，占着 python313.dll，
    # RestartManager 关不掉它，安装器会 Abort（rc=5）并整段回滚。
    stopped = updater.stop_all()
    was_running = stopped["watcher"] in ("graceful", "forced")
    if stopped["stopped"]:
        say(f"已停止 {len(stopped['stopped'])} 个占着安装目录的进程（监控/相册/守护）")
    if stopped["failed"]:
        say(f"⚠ 有 {len(stopped['failed'])} 个进程没能停下，安装可能失败（详见日志）")

    # 交接给安装器：本进程就是包内 runtime\python，RestartManager 关不掉它，
    # 自己调 setup.exe 会被判 Abort（返回 5 并回滚）。详见 updater.handoff_install。
    mode = "gui" if relaunch_gui else ("watcher" if was_running and do_restart else "none")
    say("正在交接给安装器（约 40~60 秒；日志 var\\logs\\update.log）…")
    try:
        updater.handoff_install(dest, mode=mode)
    except updater.UpdateError as exc:
        say(f"✗ {exc}")
        return done(1, error=str(exc), **found)

    say("✓ 安装器已启动，这个窗口可以关掉了。")
    if mode == "gui":
        say("  装完会自动重新打开界面。")
    elif mode == "watcher":
        say("  装完会自动把监控拉起来。")
    return done(0, handed_off=True, **found)


# ---------------------------------------------------------------- 界面支撑命令
#
# 这四个命令是给桌面界面用的：界面进程（打包后）只有 tkinter + Pillow，
# 读不了 YAML、跑不了 cv2、也不敢直接动注册表。所以一律由界面起子进程，
# 这里输出**一行 JSON** 让它解析。JSON 全部 ensure_ascii=True：
# 中文控制台是 GBK，直接吐中文可能抛 UnicodeEncodeError，把成功变成失败。


def _status_pid() -> int:
    """从 status.json 取 watcher 的 PID（读不到返回 0）。"""
    path = paths.STATE_DIR / "status.json"
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _cmd_start(argv: Sequence[str]) -> int:
    """后台启动：写 running 并交给 guardian；--foreground 保留旧行为。"""
    from . import control

    if "--foreground" in argv:
        return _run_module("watcher", [a for a in argv if a != "--foreground"])

    root = paths.PROJECT_ROOT
    python = paths.find_python()
    config = str(root / "config.yaml")
    argv_list = list(argv)
    if "--config" in argv_list:
        idx = argv_list.index("--config")
        if idx + 1 < len(argv_list):
            config = argv_list[idx + 1]

    log_path = str(root / "var" / "logs" / "guardian.log")
    (root / "var" / "logs").mkdir(parents=True, exist_ok=True)
    control.set_desired_state(root, "running", by="cli")

    cmd = [python, "-m", "roomwatch", "guard", "--config", config, "--log", log_path]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(
            cmd, cwd=str(root), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
        )
    except OSError as exc:
        print(f"启动 guardian 失败：{exc}", file=sys.stderr)
        return 1

    # 立即触发一次检查，避免用户等一个 check_seconds 周期才看到画面
    try:
        subprocess.run(
            [python, "-m", "roomwatch", "guard", "--once",
             "--config", config, "--log", log_path],
            cwd=str(root), timeout=30, capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    print("监控已启动（后台运行，由 guardian 托管）。")
    return 0


def _cmd_stop(argv: Sequence[str]) -> int:
    """优雅停止：先写 paused，再停相册与监控。"""
    from . import control, procutil

    if argv:
        raise UsageError(f"rw stop 不接受参数，收到：{' '.join(argv)}")

    root = paths.PROJECT_ROOT
    control.set_desired_state(root, "paused", by="cli")
    gallery = control.stop_gallery(root)

    pid = _status_pid()
    if not pid or not procutil.pid_alive(pid):
        # 进程已经不在，顺手清掉可能残留的请求文件
        control.clear_stop_request(root)
        print("监控没有在运行。")
        if gallery.get("method") != "already-stopped":
            print(f"相册：{gallery.get('method')}")
        return 0

    print(f"正在停止 PID {pid}（最多等 {control.DEFAULT_GRACE:.0f} 秒让录像收尾）…")
    result = control.stop_watcher(root, pid, grace=control.DEFAULT_GRACE, force=True)
    labels = {
        "graceful": "已收尾退出（录像完整）",
        "already-stopped": "本来就没在跑",
        "forced": "超时后强制结束（正在写的那段录像可能不完整）",
        "timeout": "还没停下来",
        "stuck": "强杀后仍未消失，请用任务管理器结束",
    }
    print(f"{labels.get(result['method'], result['method'])}，用时 {result['waited']:.0f} 秒")
    if gallery.get("method") != "already-stopped":
        print(f"相册：{gallery.get('method')}")
    return 0 if result["method"] in ("graceful", "already-stopped", "forced") else 1


def _items_after(argv: Sequence[str], flag: str) -> list[str]:
    items: list[str] = []
    idx = 0
    while idx < len(argv):
        if argv[idx] == flag:
            idx += 1
            while idx < len(argv) and not argv[idx].startswith("--"):
                items.append(argv[idx])
                idx += 1
            continue
        idx += 1
    return items


def _pairs_after(argv: Sequence[str], flag: str) -> dict:
    """收集 `--set 键=值 键=值` 里的键值对。值里含 = 也安全（只按第一个 = 切）。"""
    pairs: dict = {}
    for item in _items_after(argv, flag):
        if "=" not in item:
            raise UsageError(f"{flag} 之后每项都要写成 键=值，收到：{item}")
        key, _, value = item.partition("=")
        pairs[key.strip()] = value
    return pairs


def _cmd_config(argv: Sequence[str]) -> int:
    """读设置 / 改设置 / 发测试消息。界面「设置」窗口的全部后端。"""
    from . import settings

    root = paths.PROJECT_ROOT
    json_mode = "--json" in argv

    if "--help" in argv or "-h" in argv:
        print(
            "用法：rw config [--json] [--set 键=值 …] [--unset 键 …] [--test]\n"
            "  --json       机器可读输出（界面用）\n"
            "  --set        保存一批设置到 config.local.yaml（先全量校验再落盘）\n"
            "  --unset      删除本机覆盖，回落到 config.yaml 的默认值\n"
            "  --test       用当前生效的凭据发一条测试消息"
        )
        return 0

    if "--test" in argv:
        return _config_test(root, json_mode)

    if "--unset" in argv:
        keys = _items_after(argv, "--unset")
        if not keys:
            raise UsageError("--unset 后面要跟至少一个键")
        result = settings.unset(root, keys)
        if json_mode:
            print(json.dumps(result, ensure_ascii=True))
        else:
            for key in result.get("applied") or []:
                print(f"✓ 已删除 {key}")
            for key, message in (result.get("errors") or {}).items():
                print(f"✗ {key}: {message}")
        return 0 if result["ok"] else 1

    if "--set" in argv:
        changes = _pairs_after(argv, "--set")
        if not changes:
            raise UsageError("--set 后面要跟至少一个 键=值")
        result = settings.apply(root, changes)
        if json_mode:
            print(json.dumps(result, ensure_ascii=True))
        else:
            for key in result.get("applied") or []:
                print(f"✓ {key}")
            for key, message in (result.get("errors") or {}).items():
                print(f"✗ {key}: {message}")
            if result["ok"]:
                print("已保存到 config.local.yaml，重启监控后生效。")
        return 0 if result["ok"] else 1

    snap = settings.snapshot(root)
    if json_mode:
        print(json.dumps({"ok": True, **snap}, ensure_ascii=True))
        return 0

    for section in snap["sections"]:
        print(f"[{section}]")
        for key, meta in snap["labels"].items():
            if meta["section"] != section:
                continue
            if meta.get("secret"):
                info = snap["secrets"].get(key) or {}
                shown = f"已设置 {info.get('tail')}" if info.get("set") else "未设置"
            else:
                shown = snap["values"].get(key)
            print(f"  {meta['label']:<20s} {shown}")
    return 0


def _config_test(root: Path, json_mode: bool) -> int:
    """用生效配置里的凭据发一条测试消息。"""
    import io
    from contextlib import redirect_stdout

    from .config import load_config
    from .setup_push import send_test

    cfg = load_config(str(root / "config.yaml"))
    notify = cfg.get("notify") or {}
    channel = str(notify.get("channel") or "none")
    creds: dict = {}
    if channel == "wecom_bot":
        creds = {"webhook": str((notify.get("wecom_bot") or {}).get("webhook") or "")}
    elif channel == "wxpusher":
        block = notify.get("wxpusher") or {}
        creds = {"token": str(block.get("app_token") or ""), "uid": str(block.get("uid") or "")}
    elif channel == "serverchan":
        creds = {"sendkey": str((notify.get("serverchan") or {}).get("sendkey") or "")}

    missing = [key for key, value in creds.items() if not value or "replace_me" in value.lower()]
    if channel == "none" or missing:
        payload = {"ok": False, "channel": channel,
                   "message": "推送渠道是 none" if channel == "none"
                   else f"凭据不完整：缺少 {', '.join(missing)}"}
    else:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = send_test(channel, creds)
        detail = buffer.getvalue().strip()
        payload = {"ok": bool(ok), "channel": channel,
                   "message": "" if ok else (detail[-600:] or "发送失败，服务端没有返回原因")}

    if json_mode:
        print(json.dumps(payload, ensure_ascii=True))
    else:
        print(("✓ " if payload["ok"] else "✗ ") + f"{payload['channel']}："
              + ("测试消息已发出" if payload["ok"] else payload["message"]))
    return 0 if payload["ok"] else 1


def _cmd_autostart(argv: Sequence[str]) -> int:
    """开机自启：查状态、开启、关闭。"""
    from . import autostart

    root = paths.PROJECT_ROOT
    json_mode = "--json" in argv
    ok, message = True, ""

    if "--help" in argv or "-h" in argv:
        print(
            "用法：rw autostart [--json] [--enable | --disable]\n"
            "  --enable   写入 HKCU\\...\\Run\\RoomWatch（拉起守护进程）\n"
            "  --disable  删除该注册表值"
        )
        return 0

    if "--enable" in argv:
        ok, message = autostart.enable(root, paths.find_python(), root / "config.yaml")
    elif "--disable" in argv:
        ok, message = autostart.disable()

    info = autostart.status(root)
    if json_mode:
        print(json.dumps({"ok": ok, "message": message, "autostart": info}, ensure_ascii=True))
        return 0 if ok else 1

    if message:
        print(("✓ " if ok else "✗ ") + message)
    state = "已开启" if info["enabled"] else "未开启"
    print(f"当前状态：{state}" + (f"（{info['command']}）" if info["command"] else ""))
    if info["enabled"] and not info["points_to_this_install"]:
        print("⚠ 注册表里的路径不是当前安装目录，建议关掉再重新开启。")
    if not info["supported"]:
        print("（非 Windows 系统，此功能不可用）")
    return 0 if ok else 1


def _cmd_identity(argv: Sequence[str]) -> int:
    """查看/清除本人人脸模板（只回元信息，绝不输出特征）。"""
    from .config import load_config
    from .identity import clear_owner, owner_summary

    root = paths.PROJECT_ROOT
    json_mode = "--json" in argv
    cfg = load_config(str(root / "config.yaml"))
    owner = str((cfg.get("identity") or {}).get("owner_file") or
                root / "data" / "identity" / "owner.npz")

    message = ""
    ok = True
    if "--clear" in argv:
        removed = clear_owner(owner)
        ok = removed or not Path(owner).exists()
        message = "已删除本人模板" if removed else "本来就没有本人模板"

    info = owner_summary(owner)
    if json_mode:
        print(json.dumps({"ok": ok, "message": message, "identity": info}, ensure_ascii=True))
        return 0 if ok else 1

    if message:
        print(("✓ " if ok else "✗ ") + message)
    if info["registered"]:
        print(f"本人模板：{info['path']}")
        print(f"  {info['samples']} 个样本 · {info['dim']} 维 · 注册于 "
              f"{info['created_at'] or info['mtime']} · {info['size']} 字节")
        if info["error"]:
            print(f"  ⚠ 读取有问题：{info['error']}")
    else:
        print("本人模板：未注册（所有脸都会按访客处理）")
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口


def _cmd_diag(argv: Sequence[str]) -> int:
    """生成诊断包并打印路径。"""
    from . import diag

    if argv:
        raise UsageError(f"rw diag 不接受参数，收到：{' '.join(argv)}")
    path = diag.build_bundle(paths.PROJECT_ROOT)
    print(f"诊断包已生成：{path}")
    return 0


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
        if name == "diag":
            return _cmd_diag(rest)
        if name == "update":
            return _cmd_update(rest)
        if name == "version":
            return _cmd_version(rest)
        if name == "gui":
            return _cmd_gui(rest)
        if name == "start":
            return _cmd_start(rest)
        if name == "stop":
            return _cmd_stop(rest)
        if name == "config":
            return _cmd_config(rest)
        if name == "autostart":
            return _cmd_autostart(rest)
        if name == "identity":
            return _cmd_identity(rest)

        spec = COMMANDS[name]
        translated = translate(name, rest)
        return _run_module(spec.module, translated)
    except UsageError as exc:
        print(f"{exc}\n用 rw --help 查看全部命令", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
