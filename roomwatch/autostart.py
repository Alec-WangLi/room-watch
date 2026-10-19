"""开机自启：优先计划任务，失败回退到 `HKCU\\...\\Run`。

为什么不是只写 Run 键
---------------------
Run 键没有「失败自动重启」能力，也没有工作目录设置。计划任务能声明
「登录时启动 + 失败后重启」，但本机安全策略可能直接拦截 `schtasks.exe`
（所有者的机器就是如此）。所以两条路都要有，且必须写清楚当前用的是哪条。

为什么要一层 VBS
----------------
Run 项不能设置工作目录，而 `python -m roomwatch` 依赖「当前目录进 sys.path」。
VBS 里先 `CurrentDirectory` 再 `Run`，避免进程在 system32 下启动、找不到包。

为什么要清理旧入口
------------------
从 Run 升级到计划任务时，如果旧值还在，登录瞬间会同时拉起两个 guardian，
抢摄像头、重复告警。`enable()` 成功走计划任务后会主动删掉 Run 值。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape

from . import health
from .paths import PROJECT_ROOT

__all__ = [
    "IS_WINDOWS", "VBS_NAME", "RUN_KEY", "RUN_VALUE", "TASK_NAME",
    "autostart_script", "command_line", "task_xml", "status", "enable", "disable",
]

IS_WINDOWS = os.name == "nt"
VBS_NAME = "start-monitor.vbs"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RoomWatch"
TASK_NAME = "RoomWatchGuardian"
MARKER_NAME = "autostart.json"


def autostart_script(root: Path, pythonw: str, config_path: Path) -> str:
    """生成静默拉起守护进程的 VBS（无黑框）。"""
    log = Path(root) / "var" / "logs" / "guardian.log"
    return (
        "' room-watch 开机自启（由界面/向导生成）\n"
        "' 取消：界面「设置 → 自启」里关掉，或删除注册表 Run 下的 RoomWatch\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.CurrentDirectory = "{Path(root)}"\n'
        f'sh.Run """{pythonw}"" -m roomwatch guard --config ""{config_path}"" '
        f'--log ""{log}""", 0, False\n'
    )


def command_line(root: Path) -> str:
    """注册表里该写的值。带引号：安装路径可能含空格。"""
    return f'wscript.exe "{Path(root) / VBS_NAME}"'


def task_xml(root: Path, pythonw: str, config_path: Path, log_path: Path) -> str:
    """最小可用的计划任务定义：登录触发、静默、失败后重启。"""
    args = (
        f'-m roomwatch guard --config "{Path(config_path)}" '
        f'--log "{Path(log_path)}"'
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>room-watch guardian: 登录时启动并守护监控与相册</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(Path(root)))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ---------------------------------------------------------------- 注册表


def _read_run_value() -> Optional[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return str(value)
    except OSError:
        return None


def _write_run_value(value: str) -> bool:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)
    return True


def _delete_run_value() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------- 计划任务 / 标记


def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _task_exists(runner: Callable[[list[str]], tuple[int, str, str]]) -> bool:
    try:
        rc, _, _ = runner(["schtasks", "/Query", "/TN", TASK_NAME])
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


def _marker_path(root: Path) -> Path:
    return Path(root) / "var" / "state" / MARKER_NAME


def _read_marker(root: Path) -> Optional[dict]:
    try:
        data = json.loads(_marker_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_marker(root: Path, backend: str) -> None:
    try:
        health.write_json_atomic(_marker_path(root), {
            "backend": backend,
            "root": str(Path(root)),
        })
    except OSError:
        pass


# ---------------------------------------------------------------- 对外


def status(
    root: Path,
    *,
    read: Optional[Callable[[], Optional[str]]] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
    platform: Optional[str] = None,
) -> dict:
    """当前自启状态：task / run / none / conflict。"""
    plat = platform or ("win32" if IS_WINDOWS else "linux")
    script = Path(root) / VBS_NAME
    info = {
        "supported": plat == "win32",
        "enabled": False,
        "backend": "none",
        "task_name": TASK_NAME,
        "conflict": False,
        "command": "",
        "script": str(script),
        "script_exists": script.is_file(),
        "points_to_this_install": False,
        "manual": f'wscript.exe "{script}"',
    }
    if plat != "win32":
        return info

    runner = runner or _default_runner
    read = read or _read_run_value

    task_exists = _task_exists(runner)
    try:
        command = read() or ""
    except OSError:
        command = ""
    run_enabled = bool(command)

    info["command"] = command
    info["enabled"] = task_exists or run_enabled
    info["conflict"] = task_exists and run_enabled
    if info["conflict"]:
        info["backend"] = "conflict"
    elif task_exists:
        info["backend"] = "task"
    elif run_enabled:
        info["backend"] = "run"

    marker = _read_marker(root)
    marker_here = bool(
        marker and str(marker.get("root") or "").lower() == str(Path(root)).lower()
    )
    run_here = bool(command) and str(script).lower() in command.lower()
    info["points_to_this_install"] = run_here or (task_exists and (marker_here or not marker))
    return info


def enable(
    root: Path,
    python_exe: str,
    config_path: Path,
    *,
    write: Optional[Callable[[str], bool]] = None,
    delete: Optional[Callable[[], bool]] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
    platform: Optional[str] = None,
) -> tuple[bool, str]:
    """注册自启：优先计划任务，失败则回退 Run 键。"""
    plat = platform or ("win32" if IS_WINDOWS else "linux")
    if plat != "win32":
        return False, "开机自启目前只实现了 Windows；Linux 请用 systemd 单元"

    pythonw = Path(python_exe).with_name("pythonw.exe")
    if not pythonw.is_file():
        return False, f"找不到 {pythonw}（内嵌解释器应自带 pythonw.exe；分发包别用外部 python）"

    script = Path(root) / VBS_NAME
    try:
        script.write_text(autostart_script(Path(root), str(pythonw), Path(config_path)),
                          encoding="utf-8")
    except OSError as exc:
        return False, f"写启动脚本失败：{exc}"

    runner = runner or _default_runner
    xml_path = Path(root) / "var" / "state" / "room-watch-task.xml"
    task_error = ""
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(
            task_xml(Path(root), str(pythonw), Path(config_path),
                     Path(root) / "var" / "logs" / "guardian.log"),
            encoding="utf-16",
        )
        rc, _out, err = runner([
            "schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F",
        ])
        if rc == 0:
            try:
                (delete or _delete_run_value)()
            except OSError:
                pass
            _write_marker(Path(root), "task")
            return True, f"已注册计划任务 {TASK_NAME}（登录时静默启动，失败自动重启）"
        task_error = (err or f"schtasks 返回 {rc}").strip()
    except (OSError, Exception) as exc:  # noqa: BLE001 - 计划任务不可用时必须能回退
        task_error = str(exc)

    write = write or _write_run_value
    try:
        ok = write(command_line(root))
    except OSError as exc:
        return False, f"计划任务不可用（{task_error}），Run 键写入失败：{exc}"
    if not ok:
        return False, f"计划任务与 Run 键都失败：{task_error or 'Run 键写入被拒绝'}"
    _write_marker(Path(root), "run")
    return True, f"计划任务不可用（{task_error or '被策略拦截'}），已回退到 Run\\{RUN_VALUE}"


def disable(
    *,
    delete: Optional[Callable[[], bool]] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str, str]]] = None,
    platform: Optional[str] = None,
    root: Optional[Path] = None,
) -> tuple[bool, str]:
    plat = platform or ("win32" if IS_WINDOWS else "linux")
    if plat != "win32":
        return False, "开机自启目前只实现了 Windows"

    runner = runner or _default_runner
    delete = delete or _delete_run_value

    task_removed = False
    try:
        rc, _, _ = runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        task_removed = rc == 0
    except Exception:  # noqa: BLE001
        pass

    try:
        run_removed = bool(delete())
    except OSError:
        run_removed = False

    marker_root = Path(root) if root is not None else Path(PROJECT_ROOT)
    try:
        _marker_path(marker_root).unlink()
    except OSError:
        pass

    if task_removed or run_removed:
        parts = []
        if task_removed:
            parts.append("计划任务")
        if run_removed:
            parts.append(f"Run\\{RUN_VALUE}")
        return True, "已取消开机自启（" + "、".join(parts) + "）"
    return False, "本来就没有注册过（或已被手动删除）"
