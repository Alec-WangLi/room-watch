"""开机自启：写 `HKCU\\...\\Run`，状态可回读。

为什么走 `winreg` 而不是 `reg.exe`
----------------------------------
`reg add` 要解析 stdout 才知道成败，出错信息是本地化的、还常常为空；
更重要的是本机策略与沙箱都会拦外部 reg.exe（0.1.7 装完只能让用户手工运行
PowerShell 注册表命令，就是这个原因）。`winreg` 在进程内，失败抛 OSError，
能直接把「拒绝访问」这类原因告诉用户。

为什么要一层 VBS
----------------
Run 项**不能设置工作目录**，而 `python -m roomwatch` 依赖「当前目录进 sys.path」。
直接写 `pythonw -m roomwatch` 会在 system32 下启动，找不到包、进程秒退，
现象是「自启开了但什么都没有」，日志还是空的。VBS 里先 `CurrentDirectory` 再 `Run`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "IS_WINDOWS", "VBS_NAME", "RUN_KEY", "RUN_VALUE",
    "autostart_script", "command_line", "status", "enable", "disable",
]

IS_WINDOWS = os.name == "nt"
VBS_NAME = "start-monitor.vbs"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RoomWatch"


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


# ---------------------------------------------------------------- 对外


def status(
    root: Path,
    *,
    read: Optional[Callable[[], Optional[str]]] = None,
    platform: Optional[str] = None,
) -> dict:
    """当前自启状态。`points_to_this_install` 用来发现「指向旧安装目录」的残值。"""
    plat = platform or ("win32" if IS_WINDOWS else "linux")
    script = Path(root) / VBS_NAME
    info = {
        "supported": plat == "win32",
        "enabled": False,
        "command": "",
        "script": str(script),
        "script_exists": script.is_file(),
        "points_to_this_install": False,
        "manual": f'wscript.exe "{script}"',
    }
    if plat != "win32":
        return info

    read = read or _read_run_value
    try:
        command = read() or ""
    except OSError:
        command = ""
    info["command"] = command
    info["enabled"] = bool(command)
    info["points_to_this_install"] = bool(command) and str(script).lower() in command.lower()
    return info


def enable(
    root: Path,
    python_exe: str,
    config_path: Path,
    *,
    write: Optional[Callable[[str], bool]] = None,
    platform: Optional[str] = None,
) -> tuple[bool, str]:
    """注册开机自启（当前用户，不需要管理员权限）。"""
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

    write = write or _write_run_value
    try:
        ok = write(command_line(root))
    except OSError as exc:
        return False, f"注册表写入失败：{exc}"
    if not ok:
        return False, "注册表写入失败（可能被安全策略拦住）"
    return True, f"已写入 Run\\{RUN_VALUE} → {VBS_NAME}"


def disable(
    *,
    delete: Optional[Callable[[], bool]] = None,
    platform: Optional[str] = None,
) -> tuple[bool, str]:
    plat = platform or ("win32" if IS_WINDOWS else "linux")
    if plat != "win32":
        return False, "开机自启目前只实现了 Windows"

    try:
        ok = (delete or _delete_run_value)()
    except OSError as exc:
        return False, f"删除注册表值失败：{exc}"
    if ok:
        return True, "已取消开机自启（当前登录用户）"
    return False, "本来就没有注册过（或已被手动删除）"
