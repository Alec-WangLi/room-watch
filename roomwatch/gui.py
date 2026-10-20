"""桌面界面：一个窗口管住监控的启停、状态、最新抓拍和事件列表。

为什么不把界面塞进 watcher
--------------------------
监控进程是「一直跑、不能崩」的；界面是「人点来点去、随时会关」的。
两者放一个进程里，界面点崩就把录像带走了。所以这里只做**控制台**：

- 读 `var/state/status.json` 判断在线：**心跳新鲜 + PID 还活着**，缺一不可
- 读 `var/db/events.db` 显示最近事件
- 启停监控用子进程（`python -m roomwatch start`），界面退出不影响它
- 停止**不直接 taskkill**：先写 stop.request 让监控自己收尾（见 control.py）
- 配置、自启、身份走 `rw config` / `rw autostart` / `rw identity` 子进程

0.1.7 的三个真实事故（这次都钉住了）
------------------------------------
1. 停止后「启动监控」按不动：判活只看心跳，进程被杀后最长 120 秒仍报在线。
   现在 PID 死了就是离线，停止中显示「正在停止…」，两个按钮都锁住。
2. 拍预览图没反馈：probe 写 `var/media/preview.jpg`，界面只扫 `data/captures/`。
   现在两处都看，按 mtime 取新的，并在标题里标明来源与时间。
3. 注册本人不知道生不生效：原先弹一个黑框控制台，界面拿不到结果。
   现在捕获输出、解析退出码，完成后弹结果，主窗口常驻显示模板状态。

解释器说明
----------
本机主环境（含 cv2）没有 tkinter，只有系统 Python 3.13.13 自带。
所以 `rw gui` 在主环境执行时会自动改用带 tkinter 的解释器重启自己；
打包成 exe 后 tkinter 随包分发，不再有这个问题。
反过来，界面进程通常**没有 cv2**，所以拍预览图/注册要另找一个带 cv2 的解释器，
见 `_media_python()`。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import __version__
from . import control
from . import health
from . import paths
from . import procutil
from . import updater
from .labels import identity_text

# 心跳超过这个秒数就算停了。与 config.yaml 的 heartbeat.stale_after_seconds 默认值一致；
# 这里刻意不读 yaml，避免界面进程依赖 PyYAML（打包体积和启动速度都受益）。
STALE_SECONDS = 120.0
REFRESH_MS = 2000        # 状态刷新
PUMP_MS = 250            # 异步任务/停止轮询
START_TIMEOUT = 45.0     # 启动到出第一帧的容忍时间
STOP_TIMEOUT = control.DEFAULT_GRACE   # 优雅停止的等待上限

# Pillow 只用于把 jpg 画进窗口。没有它时界面照常可用，只是不显示缩略图。
try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageTk
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


@dataclass
class MonitorState:
    """界面顶栏要显示的一屏状态。

    phase 是给按钮用的粗粒度相位：
        stopped   没在跑（或进程已死）
        starting  进程在，但还没出画面 / 首次心跳
        running   心跳新鲜且进程在
        stopping  已发出停止请求，正在收尾
    """

    online: bool = False
    phase: str = "stopped"
    stale_seconds: float = 0.0
    pid: Optional[int] = None
    frames: Optional[int] = None
    events_today: Optional[int] = None
    recording: bool = False
    caption: str = ""
    last_frame: str = ""
    pending_outbox: int = 0
    last_push_success_at: Optional[str] = None
    notify_channels: Optional[int] = None
    storage_status: Optional[str] = None
    storage_days_left: Optional[float] = None
    storage_used_gb: Optional[float] = None


def _age_seconds(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(ts)).total_seconds())
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> Optional[float]:
    """状态文件里的可选数字：缺失或写坏了都当「没有」，不影响界面刷新。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_note(root: Path) -> str:
    """上次退出原因与自检失败摘要，拼在状态文案后面。"""
    state_dir = Path(root) / "var" / "state"
    notes = []
    exit_rec = health.read_json(state_dir / "last_exit.json") or {}
    if exit_rec.get("reason"):
        notes.append(f"上次退出：{exit_rec['reason']}")
    health_data = health.read_json(state_dir / "health.json") or {}
    failures = [
        item for item in (health_data.get("selfcheck") or [])
        if isinstance(item, dict) and not item.get("ok", True)
    ]
    if failures:
        names = "、".join(str(item.get("name") or "?") for item in failures)
        notes.append(f"自检失败：{names}")
    return ("（" + "；".join(notes) + "）") if notes else ""


def read_state(root: Path, alive: Optional[Callable[[int], bool]] = None) -> MonitorState:
    """读状态文件并**重算**在线判定。

    为什么不直接读 stale 字段：进程被强杀时来不及改写它，磁盘上留下的
    最后一份快照里 stale 永远还是 false（AGENTS.md F.4 的真实事故）。

    0.1.7 还多了一个坑：`taskkill /F` 之后 `last_frame_at` 仍然很新，
    界面在 120 秒里坚持「在线」，于是「启动监控」一直按不动。
    所以现在必须再确认 PID 还活着。
    """
    alive = alive or procutil.pid_alive
    state_dir = Path(root) / "var" / "state"
    note = _state_note(Path(root))
    desired = control.read_desired_state(Path(root))
    path = state_dir / "status.json"
    if not path.is_file():
        if desired == "paused":
            return MonitorState(phase="paused",
                                caption="已暂停（守护不会自动重启）" + note)
        return MonitorState(caption="未运行（找不到状态文件）" + note)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MonitorState(caption="状态文件读不了（可能正在写入）" + note)

    pid_raw = data.get("pid")
    try:
        pid = int(pid_raw) if pid_raw else None
    except (TypeError, ValueError):
        pid = None
    pid_ok = bool(pid) and alive(pid)
    # status.json 会留下上一次的 PID。进程已经不在了就不能再把它当成
    # 「还有进程」——否则停止监控后预览/注册按钮会一直灰着。
    if not pid_ok:
        pid = None

    gap = _age_seconds(data.get("last_frame_at"))
    online = pid_ok and gap is not None and gap <= STALE_SECONDS
    recording = bool(data.get("recording"))
    stopping = online and control.stop_requested(Path(root), pid or 0)
    notify_raw = data.get("notify_channels")
    try:
        notify_channels = int(notify_raw) if notify_raw is not None else None
    except (TypeError, ValueError):
        notify_channels = None

    if stopping:
        phase = "stopping"
        caption = "正在停止…（录像收尾中）"
    elif online:
        phase = "running"
        caption = "在线" + (" · 录制中" if recording else "")
    elif pid_ok:
        phase = "starting"
        caption = f"进程在，但已 {gap:.0f} 秒没有画面" if gap is not None else "启动中，暂无画面"
    elif desired == "paused":
        phase = "paused"
        caption = "已暂停（守护不会自动重启）"
    else:
        phase = "stopped"
        caption = "已停止"
    caption += note

    return MonitorState(
        online=online,
        phase=phase,
        stale_seconds=gap or 0.0,
        pid=pid,
        frames=data.get("frames"),
        events_today=data.get("events_today"),
        recording=recording,
        caption=caption,
        last_frame=str(data.get("last_frame_at") or ""),
        pending_outbox=int(data.get("pending_outbox") or 0),
        last_push_success_at=data.get("last_push_success_at"),
        notify_channels=notify_channels,
        storage_status=(str(data["storage_status"])
                        if data.get("storage_status") else None),
        storage_days_left=_opt_float(data.get("storage_days_left")),
        storage_used_gb=_opt_float(data.get("storage_used_gb")),
    )


def button_states(state: MonitorState, busy: str = "") -> tuple[str, str]:
    """返回 (启动, 停止) 两个按钮该有的 state。

    busy 期间两个都锁死：不给「再点一次」的机会——0.1.7 就是因为停止没有
    中间态，用户连点几下，每次都在 taskkill 一个正在收尾或不存在的 PID。
    """
    if busy:
        return "disabled", "disabled"
    if state.online:
        return "disabled", "normal"
    return "normal", "disabled"


def read_events(root: Path, limit: int = 10) -> list[dict]:
    """最近事件，最新在前。只读打开，绝不会顺手创建空库。"""
    db = Path(root) / "var" / "db" / "events.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # 0.1.13 之前的事件表没有 identity 列；界面只读，不能指望别人先迁移。
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        identity_col = "identity" if "identity" in cols else "NULL AS identity"
        rows = conn.execute(
            f"SELECT ts, snapshot, confidence, notified, video, {identity_col} "
            "FROM events ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def latest_snapshot(root: Path) -> Optional[Path]:
    """最新一张抓拍。按「日期目录/文件名」排序，等价于按时间排序。"""
    base = Path(root) / "data" / "captures"
    if not base.is_dir():
        return None
    files = [p for p in base.glob("*/*.jpg") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: str(p))


def preview_image(root: Path) -> tuple[Optional[Path], str]:
    """要显示的那张图：最新抓拍与 probe 预览图里 mtime 更新的那张。

    返回 (路径, 来源)，来源是 "capture" 或 "preview"；都没有时 (None, "")。
    0.1.7 只认抓拍目录，导致「拍预览图」永远看不到东西。
    """
    candidates: list[tuple[float, Path, str]] = []
    shot = latest_snapshot(root)
    if shot is not None:
        try:
            candidates.append((shot.stat().st_mtime, shot, "capture"))
        except OSError:
            pass
    preview = Path(root) / "var" / "media" / "preview.jpg"
    if preview.is_file():
        try:
            candidates.append((preview.stat().st_mtime, preview, "preview"))
        except OSError:
            pass
    if not candidates:
        return None, ""
    candidates.sort(key=lambda item: item[0])
    _, path, kind = candidates[-1]
    return path, kind


def _headless_flags(platform: Optional[str] = None) -> int:
    """Windows 下隐藏子进程的控制台窗口；非 Windows 返回 0。"""
    plat = platform or os.name
    if plat != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _run_headless(args: list[str], **kwargs):
    """所有不需要用户看见控制台的子进程都走这里。"""
    kwargs.setdefault("creationflags", _headless_flags())
    return subprocess.run(args, **kwargs)


def _update_check(python: str, project: Path, timeout: float = 180.0):
    return _run_headless(
        [python, "-m", "roomwatch", "update", "--json"],
        cwd=str(project), capture_output=True, text=True, timeout=timeout,
    )


def guard_command(python: str, root: Path, config_path: str, *, once: bool = False) -> list[str]:
    """启动守护进程；`once=True` 时立即触发一轮检查。"""
    log = Path(root) / "var" / "logs" / "guardian.log"
    cmd = [python, "-m", "roomwatch", "guard"]
    if once:
        cmd.append("--once")
    cmd += ["--config", str(config_path), "--log", str(log)]
    return cmd


def force_kill_command(pid: int) -> list[str]:
    """兜底强杀。只在优雅停止超时、用户确认后才用。"""
    return ["taskkill", "/PID", str(pid), "/F"]


def probe_command(python: str, config_path: str) -> list[str]:
    return [python, "-m", "roomwatch", "probe", "--config", str(config_path), "--json"]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def gallery_state(root: Path) -> Optional[dict]:
    """相册实际监听的地址。

    端口不再是常量：Windows 可能保留整段端口（本机 8748–8847），
    相册会改用其它端口并把实际地址写进 gallery.json。
    """
    data = health.read_json(Path(root) / "var" / "state" / "gallery.json")
    if not isinstance(data, dict):
        return None
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    host = str(data.get("host") or "127.0.0.1")
    # 绑定 0.0.0.0 时浏览器未必认这个地址；本机打开一律走回环。
    if host in ("0.0.0.0", "::"):
        url = f"http://127.0.0.1:{port}/"
    else:
        url = str(data.get("url") or f"http://{host}:{port}/")
    return {"pid": data.get("pid"), "host": host, "port": port, "url": url}


def gallery_url(root: Path) -> Optional[str]:
    info = gallery_state(root)
    return str(info["url"]) if info else None


def gallery_pid(root: Path) -> int:
    try:
        return int((Path(root) / "var" / "state" / "gallery.pid")
                   .read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _tk_candidates() -> list[str]:
    """可能带 tkinter 的解释器，按发现顺序去重。"""
    seen: list[str] = []
    raw = [
        sys.executable,
        os.environ.get("ROOMWATCH_TK_PYTHON", ""),
        str(Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe"),
        str(Path.home() / "AppData" / "Local" / "Python" / "bin" / "python.exe"),
    ]
    for item in raw:
        if item and item not in seen and Path(item).exists():
            seen.append(item)
    return seen


def _find_tk_python() -> Optional[str]:
    for exe in _tk_candidates():
        try:
            proc = _run_headless([exe, "-c", "import tkinter"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return exe
    return None


def _relaunch_with_tk(argv: Sequence[str]) -> int:
    """当前解释器没有 tkinter 时，换一个有 tkinter 的再启动自己。"""
    exe = _find_tk_python()
    if not exe:
        print("找不到带 tkinter 的 Python，无法打开图形界面。", file=sys.stderr)
        print("可选办法：安装官方 Python（含 tcl/tk），或改用 rw gallery 在浏览器里看。",
              file=sys.stderr)
        return 2

    root = paths.runtime_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    print(f"当前解释器没有 tkinter，改用：{exe}")
    try:
        proc = _run_headless([exe, "-m", "roomwatch", "gui", *argv], cwd=str(root), env=env)
        return proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2


class RoomWatchApp:
    """主窗口。所有耗时动作都起子进程/后台线程，界面线程只负责画。"""

    def __init__(self, project: Path, tk_module, ttk_module, messagebox, font_module) -> None:
        self.project = Path(project)
        self.config = self.project / "config.yaml"
        self.python = paths.find_python()
        self.tk = tk_module
        self.ttk = ttk_module
        self.messagebox = messagebox
        self._photo = None
        self._preview_key = ""
        self._preview_note = ""
        self._event_age = 0
        self._state = MonitorState()
        self._busy = ""            # "" | starting | stopping | probe | enroll | identity
        self._busy_until = 0.0
        self._stop_pid = 0
        self._tasks: "queue.Queue[tuple[Callable, dict]]" = queue.Queue()
        self._identity: dict = {}
        self._media_py: Optional[str] = None
        self._media_py_checked = False

        # 更新相关：缓存结果 + 后台检查的线程间队列
        self._update_info: dict = {}
        self._update_badge = ""
        self._checking = False
        self._check_manual = False
        self._check_queue: "queue.Queue[dict]" = queue.Queue()

        self.win = tk_module.Tk()
        self.win.title(f"房间监控 {__version__}")
        self.win.geometry("720x740")
        # 工具栏本身需要约 650px；窗口再窄就会把右侧按钮挤成 1px。
        self.win.minsize(700, 640)

        base = font_module.nametofont("TkDefaultFont")
        self.font_big = (base.actual("family"), 16, "bold")
        self.font_small = (base.actual("family"), 9)

        self._build(ttk_module)
        self.refresh()
        self._pump()
        self._init_update_state()
        self.refresh_identity()

    # ---------------- 界面搭建 ----------------

    def _build(self, ttk) -> None:
        tk = self.tk
        outer = ttk.Frame(self.win, padding=14)
        outer.pack(fill="both", expand=True)

        # 状态区
        self.status_label = tk.Label(outer, text="检查中…", font=self.font_big, anchor="w")
        self.status_label.pack(fill="x")
        self.detail_label = tk.Label(outer, text="", font=self.font_small, anchor="w", fg="#666")
        self.detail_label.pack(fill="x", pady=(2, 10))

        # 按钮区
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 8))
        self.btn_start = ttk.Button(bar, text="启动监控", command=self.on_start)
        self.btn_stop = ttk.Button(bar, text="停止监控", command=self.on_stop)
        self.btn_gallery = ttk.Button(bar, text="打开相册", command=self.on_gallery)
        self.btn_probe = ttk.Button(bar, text="拍预览图", command=self.on_probe)
        self.btn_enroll = ttk.Button(bar, text="注册本人", command=self.on_enroll)
        for btn in (self.btn_start, self.btn_stop, self.btn_gallery,
                    self.btn_probe, self.btn_enroll):
            btn.pack(side="left", padx=(0, 6))
        self.btn_update = ttk.Button(bar, text="检查更新", command=self.on_update)
        self.btn_update.pack(side="right")
        self.btn_settings = ttk.Button(bar, text="设置…", command=self.on_settings)
        self.btn_settings.pack(side="right", padx=(0, 6))

        # 身份状态（常驻一行：注册了没有、几个样本、什么时候）
        self.identity_label = tk.Label(outer, text="本人模板：读取中…", font=self.font_small,
                                       anchor="w", fg="#666")
        self.identity_label.pack(fill="x", pady=(0, 10))

        # 最新抓拍 / 预览图
        self.shot_box = ttk.LabelFrame(outer, text="最新抓拍", padding=8)
        self.shot_box.pack(fill="x", pady=(0, 10))
        self.shot_label = tk.Label(self.shot_box, text="暂无抓拍", anchor="center",
                                   bg="#f0f0ee")
        self.shot_label.pack(fill="both", expand=True)
        self.shot_label.bind("<Button-1>", lambda _e: self.on_open_latest())
        tools = ttk.Frame(self.shot_box)
        tools.pack(fill="x", pady=(8, 0))
        ttk.Button(tools, text="打开这张", command=self.on_open_latest).pack(side="left")
        ttk.Button(tools, text="抓拍目录",
                   command=lambda: self._open_dir(self.project / "data" / "captures")
                   ).pack(side="left", padx=6)
        ttk.Button(tools, text="日志",
                   command=lambda: self._open_dir(self.project / "var" / "logs")).pack(side="left")
        self.btn_diag = ttk.Button(tools, text="诊断包", command=self.on_diag)
        self.btn_diag.pack(side="left", padx=(6, 0))
        self.btn_verify = ttk.Button(tools, text="验证", command=self.on_verify)
        self.btn_verify.pack(side="left", padx=(6, 0))

        # 事件列表
        box = ttk.LabelFrame(outer, text="最近事件", padding=8)
        box.pack(fill="both", expand=True)
        columns = ("ts", "identity", "conf", "video")
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=5)
        for key, text, width in (("ts", "时间", 190), ("identity", "身份", 70),
                                 ("conf", "人体置信度", 90), ("video", "录像", 60)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        # 底栏
        # 底栏用 tk.Label：ttk.Label 不接受 fg（会抛 unknown option "-fg"）
        tk.Label(outer, text=f"项目：{self.project}   ·   解释器：{self.python}",
                 font=self.font_small, fg="#888", anchor="w", justify="left",
                 wraplength=660).pack(fill="x", pady=(8, 0))

    # ---------------- 刷新 ----------------

    def refresh(self) -> None:
        state = read_state(self.project)
        self._state = state
        color = "#0F6E56" if state.online else "#993C1D"
        if self._busy == "starting":
            self.status_label.configure(text="● 正在启动…（等第一帧）", fg="#666")
        elif self._busy == "probe":
            self.status_label.configure(text="● 正在拍预览图…", fg="#666")
        elif self._busy == "enroll":
            self.status_label.configure(text="● 正在注册本人…（请正对镜头）", fg="#666")
        elif self._busy == "verify":
            self.status_label.configure(text="● 正在验证（摄像头/推送/自检）…", fg="#666")
        else:
            self.status_label.configure(text=f"● {state.caption}", fg=color)

        bits = [f"PID {state.pid}" if state.pid else "无进程"]
        if state.pid:
            bits.append(f"最后画面 {state.stale_seconds:.0f} 秒前")
        if state.events_today is not None:
            bits.append(f"今日 {state.events_today} 次")
        if state.frames is not None:
            bits.append(f"帧 {state.frames}")
        if state.pending_outbox:
            bits.append(f"待发送 {state.pending_outbox}")
        if state.storage_used_gb is not None:
            bits.append(f"存储 {state.storage_used_gb:.1f} GB")

        storage_note = ""
        if state.storage_status == "over":
            storage_note = "存储已超过上限，正在清理最旧文件"
        elif state.storage_status == "warning" and state.storage_days_left is not None:
            storage_note = f"存储预计 {state.storage_days_left:.0f} 天后达到上限"

        if state.notify_channels == 0:
            text = "告警不会送达：没有启用任何通知渠道"
            if storage_note:
                text += " · " + storage_note
            self.detail_label.configure(text=text, fg="#993C1D")
        elif storage_note:
            self.detail_label.configure(
                text="⚠ " + storage_note + " · " + " · ".join(bits), fg="#993C1D")
        else:
            self.detail_label.configure(text=" · ".join(bits), fg="#666")

        start_state, stop_state = button_states(state, self._busy)
        self.btn_start.configure(state=start_state)
        self.btn_stop.configure(state=stop_state)
        # 摄像头同一时间只能被一个进程占用，所以运行中/忙时不让探测与注册
        # 只要还有进程在跑（哪怕没出画面），摄像头就被它占着
        media_state = "disabled" if (state.pid or self._busy) else "normal"
        self.btn_probe.configure(state=media_state)
        self.btn_enroll.configure(state=media_state)

        # 启动超时：45 秒还没出画面就提示，不要让人一直等
        if self._busy == "starting":
            if state.online:
                self._finish_busy()
            elif self._busy_until and time.monotonic() > self._busy_until:
                self._finish_busy()
                self.messagebox.showwarning(
                    "启动超时",
                    "45 秒内没有读到画面。常见原因：摄像头被别的程序占用、编号不对、\n"
                    "RTSP 地址或密码错误。详细原因见 var\\logs\\watcher.log。")

        self._event_age += 1
        if self._event_age >= 3:
            self._event_age = 0
            self._refresh_events()
        self._refresh_preview()

        self.win.after(REFRESH_MS, self.refresh)

    def _pump(self) -> None:
        """排空后台任务队列。tkinter 不是线程安全的，线程只往队列里放。"""
        while True:
            try:
                callback, payload = self._tasks.get_nowait()
            except queue.Empty:
                break
            try:
                callback(payload)
            except Exception as exc:  # noqa: BLE001 - 回调异常不许打死界面
                self.messagebox.showerror("界面错误", str(exc))
        self.win.after(PUMP_MS, self._pump)

    def _refresh_events(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in read_events(self.project, limit=10):
            ts = str(row.get("ts") or "").replace("T", " ")
            conf = row.get("confidence")
            conf_text = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
            self.tree.insert("", "end", values=(
                ts, identity_text(row.get("identity")), conf_text,
                "有" if row.get("video") else "-"))

    def _refresh_preview(self) -> None:
        path, kind = preview_image(self.project)
        try:
            mtime = path.stat().st_mtime if path else 0.0
        except OSError:
            mtime = 0.0
        key = f"{path}|{mtime:.3f}"
        if key == self._preview_key:
            return
        self._preview_key = key

        if not path:
            self.shot_box.configure(text="最新抓拍")
            self.shot_label.configure(image="", text="暂无抓拍")
            self._photo = None
            return

        stamp = datetime.fromtimestamp(mtime).strftime("%H:%M:%S") if mtime else ""
        title = "预览图" if kind == "preview" else "最新抓拍"
        self.shot_box.configure(text=f"{title} · {stamp}" if stamp else title)

        if Image is None or ImageTk is None:
            self.shot_label.configure(image="", text=f"{path.name}\n（未安装 Pillow，无法内嵌预览）")
            self._photo = None
            return
        try:
            img = Image.open(path)
            width, height = img.size
            img.thumbnail((560, 280))
            self._photo = ImageTk.PhotoImage(img)
            note = f"{path.name} · {width}×{height}"
            self.shot_label.configure(image=self._photo, text=note, compound="top",
                                      font=self.font_small)
        except Exception:  # noqa: BLE001 - 半写坏的 jpg 不该让界面崩
            self.shot_label.configure(image="", text=f"{path.name}（预览失败）")
            self._photo = None

    # ---------------- 异步执行 ----------------

    def _run_task(self, fn: Callable[[], dict], on_done: Callable[[dict], None]) -> None:
        """后台线程跑 fn，结果回主线程交给 on_done。"""

        def work() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            self._tasks.put((on_done, result))

        threading.Thread(target=work, daemon=True).start()

    def _run_cli(self, args: list[str], timeout: float = 60.0,
                 need_cv2: bool = False) -> dict:
        """跑 `rw` 子命令，解析最后一行 JSON。

        子进程一律 UTF-8：中文 Windows 的默认管道编码是 GBK，
        混着解码会得到乱码，比报错更难查。
        """
        exe = self._media_python() if need_cv2 else self.python
        if not exe:
            return {"ok": False, "error": "找不到带 OpenCV 的解释器（拍预览图/注册需要它）",
                    "rc": -1, "out": ""}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = _run_headless(
                [exe, "-m", "roomwatch", *args],
                cwd=str(self.project), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"命令超时（{timeout:.0f} 秒）", "rc": -1, "out": ""}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc), "rc": -1, "out": ""}

        out = (proc.stdout or "")
        err = (proc.stderr or "")
        data: dict = {}
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            break
        data.setdefault("rc", proc.returncode)
        data.setdefault("out", out)
        data.setdefault("err", err)
        if "ok" not in data:
            data["ok"] = proc.returncode == 0
        return data

    def _media_python(self) -> Optional[str]:
        """找一个带 cv2 的解释器（界面自己通常没有）。结果缓存。"""
        if self._media_py_checked:
            return self._media_py
        self._media_py_checked = True

        seen: list[str] = []
        for item in (os.environ.get("ROOMWATCH_PYTHON", ""),
                     paths.bundled_python() or "",
                     self.python,
                     sys.executable):
            if item and item not in seen and Path(item).is_file():
                seen.append(item)
        for exe in seen:
            try:
                proc = _run_headless([exe, "-c", "import cv2"], capture_output=True, timeout=25)
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0:
                self._media_py = exe
                break
        return self._media_py

    def _finish_busy(self) -> None:
        self._busy = ""
        self._busy_until = 0.0
        self._stop_pid = 0

    def _spawn(self, args: list[str], console: bool = False) -> None:
        flags = 0
        if os.name == "nt":
            flags = (subprocess.CREATE_NEW_CONSOLE if console
                     else subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        subprocess.Popen(
            args, cwd=str(self.project), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            close_fds=True,
        )

    # ---------------- 动作：启停 ----------------

    def on_start(self) -> None:
        if self._busy:
            return
        state = read_state(self.project)
        if state.online:
            self.messagebox.showinfo("已在运行", "监控已经在跑了，不用重复启动。")
            return
        (self.project / "var" / "logs").mkdir(parents=True, exist_ok=True)
        # 清掉上一轮可能残留的停止请求，否则新进程一起来就自己退了
        control.clear_stop_request(self.project)
        control.set_desired_state(self.project, "running", by="gui")
        try:
            # 常驻 guardian 负责后续自愈；--once 立即触发一轮，避免等一个周期
            self._spawn(guard_command(self.python, self.project, str(self.config)))
            self._spawn(guard_command(self.python, self.project, str(self.config), once=True))
        except OSError as exc:
            self.messagebox.showerror("启动失败", str(exc))
            return
        self._busy = "starting"
        self._busy_until = time.monotonic() + START_TIMEOUT
        self.refresh_soon()

    def on_stop(self) -> None:
        if self._busy:
            return
        state = read_state(self.project)
        if not state.online or not state.pid:
            self.messagebox.showinfo("没有在跑", "监控当前没有运行。")
            return
        if state.recording:
            question = ("当前正在录像。停止时会先让这段视频收尾（合并音轨、写完整 mp4），"
                        f"最多等 {STOP_TIMEOUT:.0f} 秒。\n\n现在停止吗？")
        else:
            question = f"停止监控？会先让它自己收尾，最多等 {STOP_TIMEOUT:.0f} 秒。"
        if not self.messagebox.askyesno("停止监控", question):
            return

        control.set_desired_state(self.project, "paused", by="gui")
        pid = int(state.pid)
        self._busy = "stopping"
        self._stop_pid = pid
        self.refresh_soon()

        def work() -> dict:
            gallery = control.stop_gallery(self.project)
            result = control.stop_watcher(self.project, pid, grace=STOP_TIMEOUT, force=False)
            result["gallery"] = gallery
            return result

        self._run_task(work, self._on_stop_done)

    def _on_stop_done(self, result: dict) -> None:
        method = str(result.get("method") or "")
        waited = float(result.get("waited") or 0.0)
        # 先把 PID 取出来：_finish_busy() 会把它清零，而下面的强杀分支还要用
        pid = self._stop_pid
        self._finish_busy()
        self.refresh_soon()

        if method == "graceful":
            self.messagebox.showinfo("已停止", f"监控已收尾退出（用时 {waited:.0f} 秒）。")
            return
        if method == "already-stopped":
            return
        if method == "timeout":
            if not pid:
                return
            ok = self.messagebox.askyesno(
                "还没有停下来",
                f"{STOP_TIMEOUT:.0f} 秒内没有收尾完成（可能卡在写盘或音轨合并）。\n\n"
                "要强制结束吗？正在写的这段视频可能放不了。")
            if not ok:
                return
            self._busy = "stopping"
            self.refresh_soon()

            def kill_work() -> dict:
                try:
                    _run_headless(force_kill_command(pid), capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
                gone = procutil.wait_pid_exit(pid, timeout=5.0)
                control.clear_stop_request(self.project)
                return {"ok": gone, "method": "forced" if gone else "stuck"}

            self._run_task(kill_work, self._on_kill_done)
            return
        self.messagebox.showwarning("停止结果未知", f"返回：{result}")

    def _on_kill_done(self, result: dict) -> None:
        self._finish_busy()
        self.refresh_soon()
        if result.get("ok"):
            self.messagebox.showinfo("已强制结束", "进程已结束。正在写的那段录像可能不完整。")
        else:
            self.messagebox.showerror("强制结束失败", "进程还在。可打开任务管理器手动结束，"
                                                    "或重启电脑。")

    def refresh_soon(self) -> None:
        self.win.after(200, self.refresh)

    # ---------------- 动作：相册 / 预览 / 注册 ----------------

    def on_gallery(self) -> None:
        info = gallery_state(self.project)
        if info and _port_open(int(info["port"])):
            webbrowser.open(str(info["url"]))
            return
        pid = gallery_pid(self.project)
        if not (pid and procutil.pid_alive(pid)):
            self._spawn([self.python, "-m", "roomwatch", "gallery"])
        self._open_gallery_soon(time.monotonic() + 8.0)

    def _open_gallery_soon(self, deadline: float) -> None:
        """相册启动后才知道实际端口，轮询状态文件再打开浏览器。"""
        info = gallery_state(self.project)
        if info and _port_open(int(info["port"])):
            webbrowser.open(str(info["url"]))
            return
        if time.monotonic() < deadline:
            self.win.after(300, lambda: self._open_gallery_soon(deadline))
            return
        webbrowser.open(str((info or {}).get("url") or "http://127.0.0.1:8765/"))

    def on_probe(self) -> None:
        if self._busy:
            return
        if read_state(self.project).pid:
            self.messagebox.showwarning("请先停止监控",
                                        "摄像头同一时间只能被一个进程占用，先停止监控再拍预览图。")
            return
        self._busy = "probe"
        self.refresh_soon()

        def work() -> dict:
            return self._run_cli(["probe", "--config", str(self.config), "--json"],
                                 timeout=90, need_cv2=True)

        self._run_task(work, self._on_probe_done)

    def _on_probe_done(self, result: dict) -> None:
        self._finish_busy()
        self._preview_key = ""       # 强制重画缩略图
        self.refresh_soon()
        if result.get("ok"):
            size = ""
            if result.get("width") and result.get("height"):
                size = f"{result['width']}×{result['height']}，"
            ms = result.get("ms")
            cost = f"，用时 {int(ms)} 毫秒" if isinstance(ms, (int, float)) else ""
            self.messagebox.showinfo(
                "摄像头可用",
                f"已拍到画面：{size}保存在 var\\media\\preview.jpg{cost}。\n"
                "上方缩略图就是刚拍的那张。")
            return
        tail = self._tail(result)
        self.messagebox.showerror(
            "拍预览图失败",
            (result.get("error") or tail or "没有拿到原因") +
            "\n\n常见原因：摄像头被别的程序占用（会议软件/相册）、编号不对、RTSP 地址或密码错误。\n"
            "详细日志：var\\logs\\probe.log")

    def on_diag(self) -> None:
        """生成脱敏诊断包。不阻塞界面，也不占用摄像头。"""
        if getattr(self, "_diag_running", False):
            return
        self._diag_running = True
        self.btn_diag.configure(state="disabled")

        def work() -> dict:
            return self._run_cli(["diag", "--json"], timeout=120)

        self._run_task(work, self._on_diag_done)

    def _on_diag_done(self, result: dict) -> None:
        self._diag_running = False
        if self.btn_diag.winfo_exists():
            self.btn_diag.configure(state="normal")
        path = result.get("path")
        if result.get("ok") and path:
            self.messagebox.showinfo(
                "诊断包已生成",
                f"{path}\n\n"
                "已脱敏：不含抓拍、录像、人脸模板和完整密钥。\n"
                "反馈问题时把这个 zip 发出去即可。")
            return
        self.messagebox.showerror(
            "诊断包生成失败",
            (result.get("error") or result.get("out") or "没有拿到原因"))

    def on_verify(self) -> None:
        """一次确认「真的在工作」：自检、摄像头、推送、可选心跳。"""
        if self._busy:
            return
        running = bool(read_state(self.project).pid)
        self._busy = "verify"
        self.status_label.configure(
            text="● 正在验证（摄像头/推送/自检）…", fg="#666")

        def work() -> dict:
            args = ["verify", "--json"]
            if running:
                # 监控运行中摄像头被占用；跳过它，其余照测。
                args.append("--no-camera")
            return self._run_cli(args, timeout=180, need_cv2=not running)

        self._run_task(work, self._on_verify_done)

    def _on_verify_done(self, result: dict) -> None:
        self._finish_busy()
        self.refresh_soon()
        items = result.get("items") or []
        if not items:
            self.messagebox.showerror(
                "验证失败",
                result.get("error") or result.get("out") or "没有拿到验证结果")
            return
        lines = []
        for item in items:
            mark = "–" if item.get("skipped") else ("✓" if item.get("ok") else "✗")
            lines.append(f"{mark} {item['name']}：{item.get('detail') or ''}")
        text = "\n".join(lines)
        if result.get("ok"):
            self.messagebox.showinfo(
                "验证通过", text + "\n\n监控已经真的在工作。")
        else:
            self.messagebox.showwarning(
                "还有项目没通过", text + "\n\n按上面的提示处理后重新验证。")

    def on_enroll(self) -> None:
        if self._busy:
            return
        if read_state(self.project).pid:
            self.messagebox.showwarning("请先停止监控",
                                        "摄像头同一时间只能被一个进程占用，先停止监控再注册。")
            return
        ok = self.messagebox.askyesno(
            "注册本人",
            "接下来几秒请正对摄像头、保证光线够亮。\n\n"
            "采集期间不要在画面里出现第二个人（会把别人注册成你）。\n\n开始吗？")
        if not ok:
            return
        self._busy = "enroll"
        self.refresh_soon()

        def work() -> dict:
            return self._run_cli(["enroll", "--config", str(self.config)],
                                 timeout=180, need_cv2=True)

        self._run_task(work, self._on_enroll_done)

    def on_enroll_from_latest(self) -> None:
        """没有摄像头/摄像头被占时的退路：用最新一张抓拍注册。"""
        if self._busy:
            return
        shot = latest_snapshot(self.project)
        if not shot:
            self.messagebox.showinfo("还没有抓拍", "先让人进一次门，或拍一张正脸照放进抓拍目录。")
            return
        ok = self.messagebox.askyesno(
            "用抓拍注册", f"用这张图注册本人？\n{shot}\n\n图里必须只有你一张脸。")
        if not ok:
            return
        self._busy = "enroll"
        self.refresh_soon()

        def work() -> dict:
            return self._run_cli(["enroll", "--from", str(shot), "--config", str(self.config)],
                                 timeout=120, need_cv2=True)

        self._run_task(work, self._on_enroll_done)

    def _on_enroll_done(self, result: dict) -> None:
        self._finish_busy()
        self.refresh_soon()
        self.refresh_identity()
        if result.get("ok"):
            line = ""
            for text in str(result.get("out") or "").splitlines():
                if "注册完成" in text:
                    line = text.strip()
            self.messagebox.showinfo("注册完成", line or "本人模板已更新。")
            return
        self.messagebox.showerror(
            "注册失败",
            (result.get("error") or self._tail(result) or "没有拿到原因") +
            "\n\n如果提示「样本太少」：换个光线好、正对镜头的位置重试；"
            "也可以先用「用抓拍注册」。")

    @staticmethod
    def _tail(result: dict, lines: int = 4) -> str:
        text = (result.get("out") or "") + (result.get("err") or "")
        rows = [row.strip() for row in text.strip().splitlines() if row.strip()]
        return " / ".join(rows[-lines:])

    # ---------------- 身份 / 设置 ----------------

    def refresh_identity(self) -> None:
        def work() -> dict:
            return self._run_cli(["identity", "--json"], timeout=40)

        self._run_task(work, self._on_identity)

    def _on_identity(self, result: dict) -> None:
        info = result.get("identity") or {}
        self._identity = info
        if not info:
            self.identity_label.configure(text="本人模板：读取失败（可到「设置 → 身份」查看）",
                                          fg="#993C1D")
            return
        if not info.get("registered"):
            self.identity_label.configure(
                text="本人模板：未注册 —— 现在所有脸都会按「访客」处理（会抓拍、会推送）",
                fg="#993C1D")
            return
        samples = info.get("samples") or 0
        created = str(info.get("created_at") or info.get("mtime") or "").replace("T", " ")
        tail = f" · {created}" if created else ""
        self.identity_label.configure(
            text=f"本人模板：已注册 · {samples} 个样本{tail} —— 认出你时不抓拍、不推送",
            fg="#0F6E56")

    def on_settings(self) -> None:
        from . import gui_settings

        gui_settings.open_settings(self)

    def first_run_hint(self) -> None:
        """首次启动：没有 config.local.yaml 时把用户带到设置页。

        安装器不再启动黑框配置向导；摄像头、推送、相册都在设置里完成。
        """
        if (self.project / "config.local.yaml").is_file():
            return
        self.messagebox.showinfo(
            "首次配置",
            "还没有保存过本机配置。\n\n"
            "接下来打开「设置」，按页签填好摄像头、推送和相册。\n"
            "保存后回到主界面点「验证」，确认摄像头和推送真的可用，"
            "再点「启动监控」。")
        self.on_settings()

    # ---------------- 更新 ----------------
    #
    # 界面不自己实现版本比对，一律走 `rw update --json`：版本纪律（同「主.次」
    # 只升修订号才自动装、跨次版本必须确认）只有 updater.decide 一处实现，
    # 界面再写一遍就会漂移。

    def _init_update_state(self) -> None:
        """启动先用缓存定按钮文字，缓存过期再后台查一次（不弹窗、不挡人）。"""
        cache = updater.load_check_cache(self.project)
        self._apply_update_cache(cache)
        raw = cache.get("check_interval_hours")
        interval = float(raw) if isinstance(raw, (int, float)) else 24.0
        if updater.should_auto_check(cache, interval):
            self._start_check(manual=False)

    def _apply_update_cache(self, cache: dict) -> None:
        if not cache:
            return
        self._update_info = cache
        newer = updater.available_version(cache, __version__)
        if newer:
            self._update_badge = str(newer)
            self.btn_update.configure(text=f"有新版本 {newer}")
        else:
            self._update_badge = ""
            self.btn_update.configure(text="检查更新")

    def _start_check(self, manual: bool) -> None:
        if self._checking:
            return
        self._checking = True
        self._check_manual = manual
        if manual:
            self.btn_update.configure(text="检查中…", state="disabled")

        def work() -> None:
            data: dict
            try:
                proc = _update_check(self.python, self.project, timeout=180)
                lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
                data = json.loads(lines[-1]) if lines else {}
                data.setdefault("error", "")
            except (OSError, subprocess.SubprocessError) as exc:
                data = {"error": f"检查失败：{exc}"}
            except ValueError:
                data = {"error": "检查失败：输出不是有效 JSON"}
            self._check_queue.put(data)

        threading.Thread(target=work, daemon=True).start()
        self.win.after(400, self._poll_check)

    def _poll_check(self) -> None:
        """主线程轮询检查结果（tkinter 不是线程安全的，线程只往队列里放）。"""
        try:
            data = self._check_queue.get_nowait()
        except queue.Empty:
            self.win.after(400, self._poll_check)
            return

        self._checking = False
        self.btn_update.configure(state="normal")
        manual, self._check_manual = self._check_manual, False

        error = str(data.get("error") or "")
        if error:
            self.btn_update.configure(
                text=f"有新版本 {self._update_badge}" if self._update_badge else "检查更新")
            if manual:
                self.messagebox.showwarning("检查更新失败", error)
            return

        cache = {key: data.get(key) for key in (
            "checked_at", "current", "latest", "policy", "tag", "notes",
            "channel", "channel_label", "check_interval_hours")}
        self._apply_update_cache(cache)

        newer = updater.available_version(cache, __version__)
        if not newer:
            if manual:
                label = str(cache.get("channel_label") or "稳定版")
                self.messagebox.showinfo(
                    "已是最新",
                    f"当前版本 {__version__} 就是最新版本（{label}通道）。")
            return
        if manual:
            self._ask_install(cache, newer)

    def on_update(self) -> None:
        if self._update_badge:
            newer = updater.available_version(self._update_info, __version__)
            if not newer:
                self._update_badge = ""
                self.btn_update.configure(text="检查更新")
                return
            self._ask_install(self._update_info, newer)
            return
        self._start_check(manual=True)

    def _ask_install(self, info: dict, newer) -> None:
        policy = str(info.get("policy") or "")
        if policy in ("confirm-minor", "confirm-major"):
            kind = "次版本" if policy == "confirm-minor" else "主版本"
            ok = self.messagebox.askyesno(
                f"确认{kind}升级",
                f"这是跨{kind}升级（{__version__} → {newer}）。\n"
                "按项目版本纪律，跨次版本必须由人确认，不会自动安装。\n\n继续升级吗？")
            if not ok:
                return

        notes = str(info.get("notes") or "").strip()
        tail = f"\n\n更新说明（节选）：\n{notes[:600]}" if notes else ""
        label = str(info.get("channel_label") or "")
        title = f"发现新版本 {newer}" + (f"（{label}）" if label else "")
        ok = self.messagebox.askyesno(
            title,
            f"当前 {__version__} → {newer}。{tail}\n\n"
            "点「是」立即更新：界面会关闭，更新完成后自动重新打开；\n"
            "监控进程会被短暂停止（约 1 分钟）。")
        if not ok:
            return
        self._apply_update()

    def _apply_update(self) -> None:
        """交给独立控制台进程去装。

        界面进程自己占着 `room-watch.exe`，安装器换不掉正在运行的可执行文件，
        所以必须先把 PID 交给子进程、自己退出（`--wait-exit` + `--relaunch-gui`）。
        """
        args = [self.python, "-m", "roomwatch", "update", "--yes"]
        channel = str(self._update_info.get("channel") or "")
        if channel in ("stable", "beta"):
            args += ["--channel", channel]
        args += ["--wait-exit", str(os.getpid()), "--relaunch-gui"]
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen(args, cwd=str(self.project), creationflags=flags, close_fds=True)
        except OSError as exc:
            self.messagebox.showerror("更新启动失败", str(exc))
            return
        self.status_label.configure(text="● 正在更新，界面即将关闭…", fg="#666")
        self.btn_update.configure(state="disabled")
        self.win.after(800, self.win.destroy)

    def on_open_latest(self) -> None:
        path, _kind = preview_image(self.project)
        if not path:
            self.messagebox.showinfo("还没有抓拍", "等有人进门，或先点「拍预览图」。")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            self.messagebox.showerror("打不开", str(exc))

    def _open_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            self.messagebox.showerror("打不开", str(exc))

    def run(self) -> int:
        self.win.mainloop()
        return 0


def _trace(msg: str) -> None:
    """可选追踪：`set ROOMWATCH_GUI_TRACE=1` 后把启动阶段写进 var/logs/gui-trace.log。

    打包后的窗口程序没有控制台，出问题时只能靠这个文件判断卡在哪一步。
    """
    if not os.environ.get("ROOMWATCH_GUI_TRACE"):
        return
    try:
        log = paths.runtime_root() / "var" / "logs" / "gui-trace.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def parse_args(argv: Sequence[str] | None) -> tuple[list[str], Optional[Path]]:
    """拆出 --root。

    `argv is None` 时必须读 `sys.argv` —— 打包成 exe 后入口是无参调用，
    早期版本写成 `list(argv or [])` 会静默丢掉所有命令行参数
    （表现为 `exe --selftest` 照样开窗口、卡在事件循环里）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    root: Optional[Path] = None
    if "--root" in args:
        idx = args.index("--root")
        if idx + 1 < len(args):
            root = Path(args[idx + 1]).resolve()
            del args[idx:idx + 2]
        else:
            del args[idx:idx + 1]
    return args, root


def main(argv: Sequence[str] | None = None) -> int:
    """入口。有 tkinter 就直接开窗，没有就换解释器重来。"""
    argv, explicit_root = parse_args(argv)
    _trace(f"main start argv={argv} frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")
    root = explicit_root or paths.runtime_root()

    try:
        _trace("importing tkinter")
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import messagebox, ttk
        _trace("tkinter imported")
    except ImportError:
        return _relaunch_with_tk(argv)

    if not (root / "config.yaml").is_file():
        messagebox.showerror("找不到项目", f"{root} 下没有 config.yaml。\n请把程序放在项目目录内，"
                                            "或设置 ROOMWATCH_ROOT。")
        return 2

    _trace("building app")
    app = RoomWatchApp(root, tk, ttk, messagebox, tkfont)
    _trace("app built")

    if "--selftest" in argv:
        # 不进入事件循环：建窗 → 校验关键控件 → 打开一次设置窗 → 销毁。
        # 用于构建后验证 exe 真的能起来，且版面没退化成空窗口。
        app.win.update()
        problems: list[str] = []
        if "●" not in app.status_label.cget("text"):
            problems.append("状态标签没有渲染")
        for name, btn in (("启动", app.btn_start), ("停止", app.btn_stop),
                          ("相册", app.btn_gallery), ("预览", app.btn_probe),
                          ("注册", app.btn_enroll), ("验证", app.btn_verify),
                          ("设置", app.btn_settings),
                          ("更新", app.btn_update), ("诊断", app.btn_diag)):
            if not btn.winfo_exists():
                problems.append(f"{name}按钮不存在")
            elif btn.winfo_width() <= 1 or not btn.winfo_ismapped():
                problems.append(f"{name}按钮被挤出窗口（宽度 {btn.winfo_width()}px）")
        bar = app.btn_settings.master
        if bar.winfo_reqwidth() > bar.winfo_width():
            problems.append(
                f"工具栏需要 {bar.winfo_reqwidth()}px，窗口只有 {bar.winfo_width()}px")
        if len(app.tree["columns"]) != 4:
            problems.append("事件表列数不对")
        if "本人模板" not in app.identity_label.cget("text"):
            problems.append("身份状态行没有渲染")

        # 打包后的界面必须自带 psutil：没有它，判活会回退到 tasklist，
        # 而每 2 秒一次的 tasklist 会让窗口持续闪控制台。构建自检直接拦下。
        if getattr(sys, "frozen", False):
            try:
                import psutil  # noqa: F401
            except Exception:  # noqa: BLE001
                problems.append("psutil 未打包进界面（判活会回退到 tasklist 并闪烁）")

        from . import gui_settings

        dialog = gui_settings.SettingsDialog(app, autoload=False)
        app.win.update()
        tabs = dialog.notebook.tabs()
        expected_tabs = len(gui_settings.SECTIONS)
        if len(tabs) != expected_tabs:
            problems.append(f"设置页签数量是 {len(tabs)}，应为 {expected_tabs}")
        dialog.close()

        width = app.win.winfo_width()
        height = app.win.winfo_height()
        app.win.destroy()

        log = root / "var" / "logs" / "gui-selftest.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        if problems:
            log.write_text(f"FAIL {__version__} {stamp} {'; '.join(problems)}\n", encoding="utf-8")
            print(f"界面自检失败：{'; '.join(problems)}")
            return 1
        log.write_text(f"ok {__version__} {stamp} 窗口 {width}x{height} 控件齐全\n", encoding="utf-8")
        print(f"界面自检通过（{__version__}），窗口 {width}x{height}，记录：{log}")
        return 0

    app.win.after(500, app.first_run_hint)
    app.run()
    return 0


__all__ = [
    "MonitorState", "read_state", "read_events", "latest_snapshot", "preview_image",
    "gallery_state", "gallery_url", "gallery_pid",
    "button_states", "force_kill_command", "probe_command",
    "RoomWatchApp", "main",
]
