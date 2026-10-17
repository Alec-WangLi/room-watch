"""桌面界面：一个窗口管住监控的启停、状态、最新抓拍和事件列表。

为什么不把界面塞进 watcher
--------------------------
监控进程是「一直跑、不能崩」的；界面是「人点来点去、随时会关」的。
两者放一个进程里，界面点崩就把录像带走了。所以这里只做**控制台**：

- 读 `var/state/status.json` 判断在线（读取时重算心跳，不信文件里的 stale）
- 读 `var/db/events.db` 显示最近事件
- 启停监控用子进程（`python -m roomwatch start`），界面退出不影响它
- 启停相册同上

解释器说明
----------
本机主环境（含 cv2）没有 tkinter，只有系统 Python 3.13.13 自带。
所以 `rw gui` 在主环境执行时会自动改用带 tkinter 的解释器重启自己；
打包成 exe 后 tkinter 随包分发，不再有这个问题。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from . import paths

# 心跳超过这个秒数就算停了。与 config.yaml 的 heartbeat.stale_after_seconds 默认值一致；
# 这里刻意不读 yaml，避免界面进程依赖 PyYAML（打包体积和启动速度都受益）。
STALE_SECONDS = 120.0
REFRESH_MS = 2000

# Pillow 只用于把 jpg 画进窗口。没有它时界面照常可用，只是不显示缩略图。
try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageTk
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


@dataclass
class MonitorState:
    """界面顶栏要显示的一屏状态。"""

    online: bool = False
    stale_seconds: float = 0.0
    pid: Optional[int] = None
    frames: Optional[int] = None
    events_today: Optional[int] = None
    recording: bool = False
    caption: str = ""
    last_frame: str = ""


def _age_seconds(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(ts)).total_seconds())
    except (TypeError, ValueError):
        return None


def read_state(root: Path) -> MonitorState:
    """读状态文件并**重算**在线判定。

    为什么不直接读 stale 字段：进程被强杀时来不及改写它，磁盘上留下的
    最后一份快照里 stale 永远还是 false（AGENTS.md D.2 的真实事故）。
    """
    path = Path(root) / "var" / "state" / "status.json"
    if not path.is_file():
        return MonitorState(caption="未运行（找不到状态文件）")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MonitorState(caption="状态文件读不了（可能正在写入）")

    gap = _age_seconds(data.get("last_frame_at"))
    online = gap is not None and gap <= STALE_SECONDS
    recording = bool(data.get("recording"))
    caption = "在线" if online else "已停止 / 无画面"
    if recording:
        caption += " · 录制中"

    return MonitorState(
        online=online,
        stale_seconds=gap or 0.0,
        pid=data.get("pid"),
        frames=data.get("frames"),
        events_today=data.get("events_today"),
        recording=recording,
        caption=caption,
        last_frame=str(data.get("last_frame_at") or ""),
    )


def read_events(root: Path, limit: int = 10) -> list[dict]:
    """最近事件，最新在前。只读打开，绝不会顺手创建空库。"""
    db = Path(root) / "var" / "db" / "events.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, snapshot, confidence, notified, video FROM events "
            "ORDER BY ts DESC, id DESC LIMIT ?",
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


def start_command(python: str, root: Path, config_path: str) -> list[str]:
    log = Path(root) / "var" / "logs" / "watcher.log"
    return [python, "-m", "roomwatch", "start", "--config", str(config_path), "--log", str(log)]


def stop_command(pid: int) -> list[str]:
    return ["taskkill", "/PID", str(pid), "/F"]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


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
            proc = subprocess.run([exe, "-c", "import tkinter"], capture_output=True, timeout=20)
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
        proc = subprocess.run([exe, "-m", "roomwatch", "gui", *argv], cwd=str(root), env=env)
        return proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2


class RoomWatchApp:
    """主窗口。所有耗时动作都起子进程，界面线程只负责画。"""

    def __init__(self, project: Path, tk_module, ttk_module, messagebox, font_module) -> None:
        self.project = Path(project)
        self.config = self.project / "config.yaml"
        self.python = paths.find_python()
        self.tk = tk_module
        self.messagebox = messagebox
        self._photo = None
        self._last_snapshot_key = ""
        self._event_age = 0

        self.win = tk_module.Tk()
        self.win.title(f"房间监控 {__version__}")
        self.win.geometry("620x560")
        self.win.minsize(560, 480)

        base = font_module.nametofont("TkDefaultFont")
        self.font_big = (base.actual("family"), 16, "bold")
        self.font_small = (base.actual("family"), 9)

        self._build(ttk_module)
        self.refresh()

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
        bar.pack(fill="x", pady=(0, 12))
        self.btn_start = ttk.Button(bar, text="启动监控", command=self.on_start)
        self.btn_stop = ttk.Button(bar, text="停止监控", command=self.on_stop)
        self.btn_gallery = ttk.Button(bar, text="打开相册", command=self.on_gallery)
        self.btn_probe = ttk.Button(bar, text="拍预览图", command=self.on_probe)
        for btn in (self.btn_start, self.btn_stop, self.btn_gallery, self.btn_probe):
            btn.pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="注册本人", command=self.on_enroll).pack(side="left")

        # 最新抓拍
        shot = ttk.LabelFrame(outer, text="最新抓拍", padding=8)
        shot.pack(fill="x", pady=(0, 12))
        self.shot_label = tk.Label(shot, text="暂无抓拍", anchor="center", height=8, bg="#f0f0ee")
        self.shot_label.pack(fill="both", expand=True)
        self.shot_label.bind("<Button-1>", lambda _e: self.on_open_latest())
        tools = ttk.Frame(shot)
        tools.pack(fill="x", pady=(8, 0))
        ttk.Button(tools, text="打开最新抓拍", command=self.on_open_latest).pack(side="left")
        ttk.Button(tools, text="抓拍目录", command=lambda: self._open_dir(self.project / "data" / "captures")).pack(side="left", padx=6)
        ttk.Button(tools, text="日志", command=lambda: self._open_dir(self.project / "var" / "logs")).pack(side="left")

        # 事件列表
        box = ttk.LabelFrame(outer, text="最近事件", padding=8)
        box.pack(fill="both", expand=True)
        columns = ("ts", "conf", "video")
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=6)
        for key, text, width in (("ts", "时间", 190), ("conf", "置信度", 80), ("video", "录像", 70)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        # 底栏
        # 底栏用 tk.Label：ttk.Label 不接受 fg（会抛 unknown option "-fg"）
        tk.Label(outer, text=f"项目：{self.project}   ·   解释器：{self.python}",
                 font=self.font_small, fg="#888", anchor="w").pack(fill="x", pady=(8, 0))

    # ---------------- 刷新 ----------------

    def refresh(self) -> None:
        state = read_state(self.project)
        color = "#0F6E56" if state.online else "#993C1D"
        self.status_label.configure(text=f"● {state.caption}", fg=color)

        bits = [f"PID {state.pid}", f"最后画面 {state.stale_seconds:.0f} 秒前" if state.pid else "无进程"]
        if state.events_today is not None:
            bits.append(f"今日 {state.events_today} 次")
        if state.frames is not None:
            bits.append(f"帧 {state.frames}")
        self.detail_label.configure(text=" · ".join(bits))

        self.btn_start.configure(state="disabled" if state.online else "normal")
        self.btn_stop.configure(state="normal" if state.online else "disabled")

        self._event_age += 1
        if self._event_age >= 3:
            self._event_age = 0
            self._refresh_events()
        self._refresh_snapshot()

        self.win.after(REFRESH_MS, self.refresh)

    def _refresh_events(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in read_events(self.project, limit=10):
            ts = str(row.get("ts") or "").replace("T", " ")
            conf = row.get("confidence")
            conf_text = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
            self.tree.insert("", "end", values=(ts, conf_text, "有" if row.get("video") else "-"))

    def _refresh_snapshot(self) -> None:
        path = latest_snapshot(self.project)
        key = str(path or "")
        if key == self._last_snapshot_key:
            return
        self._last_snapshot_key = key

        if not path:
            self.shot_label.configure(image="", text="暂无抓拍")
            self._photo = None
            return
        if Image is None or ImageTk is None:
            self.shot_label.configure(image="", text=f"{path.name}\n（未安装 Pillow，无法内嵌预览）")
            return
        try:
            img = Image.open(path)
            img.thumbnail((520, 260))
            self._photo = ImageTk.PhotoImage(img)
            self.shot_label.configure(image=self._photo, text="")
        except Exception:  # noqa: BLE001 - 半写坏的 jpg 不该让界面崩
            self.shot_label.configure(image="", text=f"{path.name}（预览失败）")

    # ---------------- 动作 ----------------

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

    def on_start(self) -> None:
        state = read_state(self.project)
        if state.online:
            self.messagebox.showinfo("已在运行", "监控已经在跑了，不用重复启动。")
            return
        (self.project / "var" / "logs").mkdir(parents=True, exist_ok=True)
        self._spawn(start_command(self.python, self.project, str(self.config)))
        self.status_label.configure(text="● 正在启动…", fg="#666")
        self.win.after(2000, self.refresh)

    def on_stop(self) -> None:
        state = read_state(self.project)
        if not state.online or not state.pid:
            self.messagebox.showinfo("没有在跑", "监控当前没有运行。")
            return
        if state.recording:
            ok = self.messagebox.askyesno(
                "正在录像", "当前可能正在写 mp4。强停会让这段视频不可播放，仍然停止吗？")
            if not ok:
                return
        elif not self.messagebox.askyesno("确认停止", f"终止监控进程 PID {state.pid}？"):
            return
        try:
            subprocess.run(stop_command(state.pid), capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            self.messagebox.showerror("停止失败", str(exc))
            return
        self.win.after(800, self.refresh)

    def on_gallery(self) -> None:
        if not _port_open(8765):
            self._spawn([self.python, "-m", "roomwatch", "gallery"])
            self.win.after(900, lambda: webbrowser.open("http://127.0.0.1:8765/"))
        else:
            webbrowser.open("http://127.0.0.1:8765/")

    def on_probe(self) -> None:
        log = self.project / "var" / "logs" / "probe.log"
        with open(log, "a", encoding="utf-8") as fh:
            subprocess.Popen(
                [self.python, "-m", "roomwatch", "probe", "--config", str(self.config)],
                cwd=str(self.project), stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            )
        self.messagebox.showinfo("拍摄中", "约 2 秒后刷新缩略图；失败可看 var/logs/probe.log。")
        self._last_snapshot_key = ""
        self.win.after(2500, self.refresh)

    def on_enroll(self) -> None:
        if read_state(self.project).online:
            self.messagebox.showwarning("请先停止监控", "摄像头同一时间只能被一个进程占用，先停止监控再注册。")
            return
        self._spawn([self.python, "-m", "roomwatch", "enroll", "--config", str(self.config)], console=True)

    def on_open_latest(self) -> None:
        path = latest_snapshot(self.project)
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
        # 不进入事件循环：建窗 → 跑一次刷新 → 销毁。用于构建后验证 exe 真的能起来。
        app.win.update()
        app.win.destroy()
        log = root / "var" / "logs" / "gui-selftest.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"ok {__version__} {datetime.now().isoformat(timespec='seconds')}\n",
                       encoding="utf-8")
        print(f"界面自检通过（{__version__}），记录：{log}")
        return 0

    app.run()
    return 0


__all__ = [
    "MonitorState", "read_state", "read_events", "latest_snapshot",
    "start_command", "stop_command", "RoomWatchApp", "main",
]
