"""首次配置向导：装完双击 `配置.bat`，回答四个问题就能用。

为什么不能靠「编辑 config.yaml」
--------------------------------
主配置是入库文件，注释详细但满屏缩进；让客户改 `camera.source` 这种键，
出错的方式非常一致：改成 `0`（字符串）而不是 `0`（整数）、把 RTSP 密码
写进主配置、或者路径里带了中文全角冒号。结果是进程能起、但摄像头打不开，
而且没有任何报错指向「你填错了」。

所以这里只做三件事：
1. 用选择题收集**最少**的信息（摄像头、微信 webhook、相册密码、开机自启）；
2. 把答案写进 `config.local.yaml`（与 `rw push` 同一套覆盖机制，不入库）；
3. 立刻验证一次（探测摄像头 / 发测试消息），失败就说清怎么修。

可测试性
--------
`parse_camera_source` / `build_local` / `merge_local` 都是纯函数，
不碰 stdin、不碰网络、不碰注册表；交互与副作用都在 `main()` 里。
这样单测能在无摄像头、无网络的机器上跑完整逻辑。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from .autostart import disable as disable_autostart
from .autostart import enable as enable_autostart
from .paths import PROJECT_ROOT, bundled_python, find_python
from .setup_push import normalize_wecom_webhook
from . import procutil

LOCAL_NAME = "config.local.yaml"
MAIN_NAME = "config.yaml"

LOCAL_HEADER = """# 本机配置覆盖（不入库、不要提交）
# 由界面「设置」/ `rw setup` / `rw push` 生成：只写与本机相关的差异，其余键仍以 config.yaml 为准。
# 为什么不写进 config.yaml：升级安装包会整体覆盖主配置，写那里的设置会丢。
# 想改检测灵敏度、录像时长这类参数，请改 config.yaml，不要在这里重复写。
"""

_NET_PREFIXES = ("rtsp://", "rtmp://", "http://", "https://")

def parse_camera_source(text: str) -> dict:
    """把用户输入翻译成 `camera` 覆盖块。

    支持的三种写法（客户实际会遇到的三种情况）：
    - `0` / `1`……        本机 USB / 内置摄像头编号 → 写 index
    - `rtsp://…`         网络摄像头 → 写 source（密码留在这台机器上）
    - 已存在的文件路径    离线调试用的录像 → 写 source

    解析失败抛 ValueError 并说明原因，绝不「静默当 0 处理」——
    那样用户会以为配好了，其实一直在拍另一台设备。
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return {"index": 0, "source": None}

    if raw.isdigit():
        return {"index": int(raw), "source": None}

    lowered = raw.lower()
    if lowered.startswith(_NET_PREFIXES):
        # 只做形状检查：真正的连通性由 probe 验证
        if "://" not in raw or len(raw.split("://", 1)[1]) < 3:
            raise ValueError(f"地址不完整：{raw}")
        return {"source": raw, "backend": "ffmpeg"}

    candidate = Path(raw)
    if candidate.is_file():
        return {"source": str(candidate.resolve())}

    raise ValueError(
        f"认不出这个摄像头来源：{raw}\n"
        "  本机摄像头填编号（0、1……）；网络摄像头填 rtsp:// 开头的地址；\n"
        "  文件路径必须是已存在的文件。"
    )


def build_local(
    camera: Optional[dict] = None,
    webhook: str = "",
    gallery_token: str = "",
    allow_lan: bool = False,
) -> dict:
    """拼出要写进 config.local.yaml 的差异块。只包含用户真的提供了的项。"""
    out: dict = {}
    if camera:
        out["camera"] = dict(camera)
    hook = normalize_wecom_webhook(webhook)
    if hook:
        out["notify"] = {"channel": "wecom_bot", "wecom_bot": {"webhook": hook}}
    token = (gallery_token or "").strip()
    if token:
        out["gallery"] = {"token": token}
        if allow_lan:
            # 绑 0.0.0.0 但没有 token 会被 gallery 拒绝启动，所以这个开关
            # 只在有 token 时才生效（前面的 if 已经保证了这一点）
            out["gallery"]["host"] = "0.0.0.0"
    return out


def merge_local(path: Path, data: dict) -> dict:
    """把 data 深合并进已有的 local 配置并返回合并结果。

    合并而不是覆盖：用户先跑 `rw push` 配了推送，再跑 `rw setup` 配摄像头，
    两次都不能把对方的键冲掉。
    """
    existing: dict = {}
    if path.is_file():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    def _merge(base: dict, extra: dict) -> dict:
        out = dict(base)
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _merge(out[key], value)
            else:
                out[key] = value
        return out

    merged = _merge(existing, data)
    path.write_text(
        LOCAL_HEADER + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return merged


# 自启的实现在 autostart.py（界面「设置 → 自启」共用同一份逻辑）。
# 这里保留同名入口，历史调用方（`rw setup` 的交互流程）不用改。
def install_autostart(root: Path, python_exe: str, config_path: Path) -> tuple[bool, str]:
    """注册开机自启（当前用户，不需要管理员权限）。返回 (是否成功, 说明)。"""
    return enable_autostart(root, python_exe, config_path)


def remove_autostart() -> tuple[bool, str]:
    """取消开机自启。"""
    return disable_autostart()


# ---------------------------------------------------------------- 交互


def ask(prompt: str, default: str = "") -> str:
    try:
        text = input(prompt).strip()
    except EOFError:
        return default
    return text or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    got = ask(f"{prompt} [{hint}]: ", "")
    if not got:
        return default
    return got.lower() in ("y", "yes", "是", "1")


def _interpreter() -> str:
    """用哪个解释器做验证。分发包里必须用内嵌的那个。"""
    return bundled_python() or find_python()


def probe_camera(config_path: Path) -> tuple[bool, str]:
    """跑一次 `--probe` 验证摄像头，返回 (是否成功, 输出尾巴)。"""
    exe = _interpreter()
    root = config_path.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(
            [exe, "-m", "roomwatch", "probe", "--config", str(config_path)],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=90,
            creationflags=procutil.no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"启动探测失败：{exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    tail = " / ".join(line.strip() for line in output.strip().splitlines()[-3:])
    return proc.returncode == 0, tail


def summarize(config_path: Path) -> list[str]:
    """打印当前生效配置的关键几项（叠加 local 之后的结果）。"""
    from .config import load_config

    cfg = load_config(str(config_path))
    camera = cfg.get("camera") or {}
    source = camera.get("source")
    if source:
        shown = str(source)
        if "@" in shown and "://" in shown:  # RTSP 地址不带密码
            head, tail = shown.split("://", 1)
            shown = f"{head}://***@{tail.split('@', 1)[-1]}"
        cam = shown
    else:
        cam = f"本机摄像头 #{camera.get('index', 0)}"

    notify = cfg.get("notify") or {}
    channel = notify.get("channel") or "（未配置）"
    gallery = cfg.get("gallery") or {}
    token = "已设置" if gallery.get("token") else "未设置（仅本机可访问）"

    return [
        f"摄像头    : {cam}",
        f"推送渠道  : {channel}",
        f"相册密码  : {token}",
        f"配置文件  : {config_path}",
    ]


def _print_intro(root: Path) -> None:
    print("=" * 58)
    print(" room-watch 首次配置")
    print("=" * 58)
    print(f"安装目录：{root}")
    print("四个问题，一路回车用默认值即可。\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="首次配置向导")
    ap.add_argument("--root", default=str(PROJECT_ROOT), help="安装目录（默认自动识别）")
    ap.add_argument("--camera", help="摄像头：0/1… 或 rtsp://… 或视频文件路径")
    ap.add_argument("--webhook", help="企业微信机器人 webhook（可只给 key）")
    ap.add_argument("--gallery-token", help="相册访问密码（局域网访问时必填，至少 12 位）")
    ap.add_argument("--allow-lan", action="store_true",
                    help="允许局域网/手机访问相册（需要同时给 --gallery-token）")
    ap.add_argument("--autostart", action="store_true", help="注册开机自启")
    ap.add_argument("--no-autostart", action="store_true", help="不注册开机自启")
    ap.add_argument("--no-probe", action="store_true", help="跳过摄像头探测")
    ap.add_argument("--no-test", action="store_true", help="跳过推送测试消息")
    ap.add_argument("--check", action="store_true", help="只打印当前生效配置")
    ap.add_argument("--remove-autostart", action="store_true", help="取消开机自启")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = root / MAIN_NAME
    local_path = root / LOCAL_NAME

    if args.check:
        if not config_path.is_file():
            print(f"找不到 {config_path}，安装目录不对？")
            return 2
        print("当前生效配置：")
        for line in summarize(config_path):
            print("  " + line)
        return 0

    if args.remove_autostart:
        ok, msg = remove_autostart()
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1

    if not config_path.is_file():
        print(f"找不到 {config_path}。请在安装目录里运行本向导（或用 --root 指定）。")
        return 2

    interactive = args.camera is None and args.webhook is None

    if interactive:
        _print_intro(root)
        print("1) 摄像头")
        print("   · 本机 USB / 笔记本内置：填 0（不对就试 1、2）")
        print("   · 网络摄像头：填 rtsp://用户名:密码@192.168.1.64:554/...")
        print("   · 手头没有设备：先回车，之后用 `rw setup --check` 再配")
        raw_camera = ask("   摄像头 [0]: ", "0")
        print("\n2) 微信推送（企业微信机器人 webhook，直接粘整条地址）")
        raw_webhook = ask("   webhook（回车跳过）: ", "")
        print("\n3) 相册密码（只在局域网访问时需要；回车跳过＝只允许本机看）")
        raw_token = ask("   访问密码（回车跳过）: ", "")
    else:
        raw_camera = args.camera or "0"
        raw_webhook = args.webhook or ""
        raw_token = args.gallery_token or ""

    try:
        camera = parse_camera_source(raw_camera)
    except ValueError as exc:
        print(f"\n✗ {exc}")
        return 2

    token = (raw_token or "").strip()
    if token and len(token) < 12:
        # 提前拦住：gallery 启动时会以「token 太短」拒绝绑非回环地址，
        # 到这里才说，用户已经点过一圈界面的「相册」按钮了。
        print(f"\n✗ 相册密码只有 {len(token)} 位，至少 12 位（房间画面不能裸奔）。")
        return 2

    allow_lan = bool(args.allow_lan)
    if token and interactive and not args.allow_lan:
        allow_lan = ask_yes("   允许手机在同一局域网访问相册？", True)

    data = build_local(camera, raw_webhook, token, allow_lan)
    merge_local(local_path, data)
    print(f"\n✓ 已写入 {local_path.name}")

    if not args.no_probe:
        print("\n正在验证摄像头（约 3~10 秒）…")
        ok, tail = probe_camera(config_path)
        if ok:
            print(f"✓ 摄像头可用。预览图在 data\\captures\\ 下。{('  ' + tail) if tail else ''}")
        else:
            print(f"✗ 摄像头没打开：{tail or '无输出'}")
            print("  检查：设备是否被别的程序占用（相册/会议软件）、编号是否正确、")
            print("  RTSP 地址与密码是否正确。改完重跑 `rw setup` 即可。")

    if data.get("notify") and not args.no_test:
        print("\n正在发送测试消息…")
        from .setup_push import send_test

        hook = data["notify"]["wecom_bot"]["webhook"]
        if send_test("wecom_bot", {"webhook": hook}):
            print("✓ 手机上应该收到一条测试消息。")
        else:
            print("✗ 没发出去。webhook 粘错的话重跑 `rw setup --webhook ...`。")

    do_autostart = args.autostart or (
        interactive and ask_yes("\n4) 开机自动启动监控（推荐）？", True)
    )
    if do_autostart and not args.no_autostart:
        ok, msg = install_autostart(root, _interpreter(), config_path)
        print(("✓ " if ok else "✗ ") + msg)

    print("\n" + "-" * 58)
    for line in summarize(config_path):
        print("  " + line)
    print("-" * 58)
    print("下一步：")
    print("  · 双击 `启动.bat`（或 room-watch.exe）打开界面，点「启动」")
    print("  · 相册：界面里点「相册」，或运行 `rw gallery`")
    print("  · 随时复查：`rw setup --check`；取消自启：`rw setup --remove-autostart`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
