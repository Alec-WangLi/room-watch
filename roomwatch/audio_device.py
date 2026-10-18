"""系统输出音量：查与调。

## 先分清两件事（0.1.7 踩过的坑）

**录音走麦克风输入，跟系统播放音量完全无关。** 0.1.7 把「提示音太小」的问题
用「抬高系统主音量」来解决，结果用户发现每次开始监控自己电脑的音量就被改成
70%（日志：`主音量 2.0% → 70.0%`），而进程被强杀时还原代码没机会跑，
音量就永久停在那里。

所以现在分三档（`voice.volume_mode`）：

    off      完全不碰（默认）。你的音量归你管
    session  只把本进程在「音量合成器」里的那条拉到 100%，不动系统主音量
    master   旧行为：主音量保底到 ensure_system_volume。界面上有明确警示

## 模块职责

1. `describe()`  把主音量、静音状态、输出设备、应用级音量全列出来
2. `set_master_percent()` 改主音量
3. `VolumeGuard` 按上面三档策略做事；`master` 档退出时还原，
   并把原值先写进 `var/state/volume_backup.json`，万一被强杀，
   下次启动能靠 `restore_backup()` 补还原。

Windows 专有；非 Windows 或没装 pycaw 时所有函数安全降级为"不可用"。
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("room-watch")

DEFAULT_ENSURE = 70.0
MODES = ("off", "session", "master")


def available() -> bool:
    """pycaw 是否可用（装了才能查/改系统音量）。"""
    try:
        import pycaw  # noqa: F401
    except Exception:
        return False
    return True


def _endpoint():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def get_master_percent() -> Optional[float]:
    """当前主音量百分比（0~100）。不可用时返回 None。"""
    try:
        return round(float(_endpoint().GetMasterVolumeLevelScalar()) * 100.0, 1)
    except Exception:
        return None


def is_muted() -> Optional[bool]:
    try:
        return bool(_endpoint().GetMute())
    except Exception:
        return None


def set_master_percent(percent: float, force_unmute: bool = True) -> bool:
    """设置主音量。原本静音时默认顺手解除静音——静音状态下调音量毫无意义。"""
    try:
        ep = _endpoint()
        ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100.0)), None)
        if force_unmute and ep.GetMute():
            ep.SetMute(False, None)
        return True
    except Exception as exc:
        log.warning("设置主音量失败: %s", exc)
        return False


RENDER_PREFIX = "{0.0.0."   # 输出设备；采集设备（麦克风）是 {0.0.1.


def list_devices(only_active: bool = True) -> list[dict]:
    """列出**输出**设备及其音量。

    只保留输出设备（id 形如 `{0.0.0.…}`）：这份列表是用来排查
    「声音到底去哪了」的，塞一堆麦克风只会淹没有用信息。

    顺带能发现两个经典事故：默认设备是个没接音箱的 HDMI 显示器，
    或者默认设备被系统禁用了。
    """
    out: list[dict] = []
    try:
        from pycaw.pycaw import AudioUtilities

        default_id = AudioUtilities.GetSpeakers().id
        seen = set()
        for dev in AudioUtilities.GetAllDevices():
            if not str(dev.id).startswith(RENDER_PREFIX):
                continue  # 麦克风之类，跳过

            state = getattr(dev, "state", None)
            active = "Active" in str(state) if state is not None else True
            if only_active and not active:
                continue

            name = str(dev.id)
            try:
                friendly = dev.FriendlyName
                if isinstance(friendly, str) and friendly.strip():
                    name = friendly.strip()
            except Exception:
                pass
            if name in seen:
                continue
            seen.add(name)

            item = {"id": dev.id, "name": name, "default": dev.id == default_id}
            try:
                item["percent"] = round(
                    float(dev.EndpointVolume.GetMasterVolumeLevelScalar()) * 100.0, 1
                )
                item["muted"] = bool(dev.EndpointVolume.GetMute())
            except Exception:
                item["percent"] = None
            out.append(item)

        # 默认设备排最前，然后按音量能读到的排前面
        out.sort(key=lambda d: (not d["default"], d.get("percent") is None, d["name"]))
    except Exception as exc:
        log.debug("枚举输出设备失败: %s", exc)
    return out


def list_app_sessions() -> list[dict]:
    """各应用自己的音量条（Windows 音量合成器里那些）。

    python.exe 的那一条如果被调低或被静音，我们播什么都是白播。
    """
    out: list[dict] = []
    try:
        from pycaw.pycaw import AudioUtilities

        for s in AudioUtilities.GetAllSessions():
            name = "(系统声音)"
            if s.Process is not None:
                try:
                    name = s.Process.name()
                except Exception:
                    name = "(未知进程)"
            try:
                vol = round(float(s.SimpleAudioVolume.GetMasterVolume()) * 100.0, 1)
                muted = bool(s.SimpleAudioVolume.GetMute())
            except Exception:
                continue
            out.append({"name": name, "percent": vol, "muted": muted})
    except Exception as exc:
        log.debug("枚举应用会话失败: %s", exc)
    return out


def describe() -> str:
    """给人看的一段诊断文字。"""
    if not available():
        return "未安装 pycaw，无法读取系统音量。需要时执行：pip install pycaw comtypes"

    lines = []
    master = get_master_percent()
    muted = is_muted()
    lines.append(f"主音量      : {master}%" if master is not None else "主音量      : 读取失败")
    lines.append(f"静音        : {'是（声音根本不会出来）' if muted else '否'}")

    if master is not None:
        if muted:
            lines.append("            ⚠ 系统处于静音状态，先解除静音再谈音量大小")
        elif master < 30:
            lines.append(f"            ⚠ 主音量只有 {master}%，"
                         f"相当于衰减 {20 * math.log10(master / 100):.0f} dB。"
                         "这才是「怎么调都小」的主因")
        elif master < 60:
            lines.append(f"            主音量偏低（{master}%），可以再抬一点")

    devs = list_devices()
    default = next((d for d in devs if d["default"]), None)
    if default is not None:
        vol = f"{default['percent']}%" if default.get("percent") is not None else "读不到"
        lines.append(f"默认输出    : {default['name']}  ({vol})")
        if default.get("muted"):
            lines.append("            ⚠ 默认设备被静音了")
        lines.append("            ↑ 声音只会从这里出来。名字看着不对（比如是个"
                     "显示器/HDMI），说明声音去了别处")
    else:
        lines.append("默认输出    : 读取失败")

    others = [d for d in devs if not d["default"] and d.get("percent") is not None]
    if others:
        lines.append(f"其他在线的输出设备（{len(others)} 个，仅供参考）:")
        for d in others[:6]:
            lines.append(f"  {d['name'][:44]:<46s} {d['percent']:>5.1f}%"
                         + ("  静音" if d.get("muted") else ""))

    apps = [a for a in list_app_sessions()
            if a["name"].lower() in ("python.exe", "pythonw.exe")]
    if apps:
        lines.append("本程序音量  :")
        for a in apps:
            lines.append(f"  {a['name']:<46s} {a['percent']:>5.1f}%"
                         + ("  静音（必须解除，否则播报无声）" if a["muted"] else ""))
    else:
        lines.append("本程序音量  : 还没有音频会话（没播过声音，属正常）")
    return "\n".join(lines)


def resolve_mode(cfg: Optional[dict]) -> str:
    """决定音量策略。

    兼容老配置：只写了 `ensure_system_volume: 70`、没有 `volume_mode` 的，
    按 `master` 解释——升级不该让老用户的行为悄悄变样。
    """
    cfg = cfg or {}
    raw = cfg.get("volume_mode")
    if isinstance(raw, bool):
        # YAML 1.1 会把裸 off/on 解析成布尔，别让它变成 "false"
        return "master" if raw else "off"
    mode = str(raw or "").strip().lower()
    if mode in MODES:
        return mode
    try:
        ensure = float(cfg.get("ensure_system_volume") or 0)
    except (TypeError, ValueError):
        ensure = 0.0
    return "master" if ensure > 0 else "off"


def set_app_session_percent(pid: int, percent: float) -> int:
    """把指定进程在「音量合成器」里的那条音轨设成 percent，返回改了几条。

    这是「不碰系统音量」的关键：只动自己那条，用户的播放器音量不受影响。
    进程还没播过声音时没有会话，返回 0 属正常——播报前会再试一次。
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return 0

    changed = 0
    try:
        for session in AudioUtilities.GetAllSessions():
            proc = getattr(session, "Process", None)
            if proc is None or getattr(proc, "pid", None) != int(pid):
                continue
            try:
                volume = session.SimpleAudioVolume
                volume.SetMasterVolume(max(0.0, min(1.0, percent / 100.0)), None)
                if volume.GetMute():
                    volume.SetMute(0, None)
                changed += 1
            except Exception:  # noqa: BLE001 - 单条会话失败不影响其它
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("设置本程序音轨失败: %s", exc)
    return changed


def write_backup(path: Path, percent: float, muted: bool, pid: int) -> None:
    """改主音量前先把原值记下来，给「被强杀」留一条后路。"""
    payload = {
        "percent": percent,
        "muted": bool(muted),
        "pid": int(pid),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.debug("写音量备份失败: %s", exc)


def restore_backup(path: Path, alive=None) -> str:
    """上次被强杀留下的音量备份：确认那个进程真不在了，就还原并清理。

    返回一句人话；没有可做的事（没备份 / 进程还在 / 还原失败）返回空串。
    """
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return ""

    pid = int(data.get("pid") or 0)
    if alive is None:
        from .procutil import pid_alive as alive_check

        alive = alive_check
    try:
        if pid and alive(pid):
            return ""      # 还在跑，说明它是当前实例，别动它
    except Exception:  # noqa: BLE001
        return ""

    if not available():
        return ""
    try:
        percent = float(data.get("percent") or 0)
    except (TypeError, ValueError):
        percent = 0.0
    if not set_master_percent(percent, force_unmute=False):
        return ""
    if data.get("muted"):
        try:
            _endpoint().SetMute(True, None)
        except Exception:  # noqa: BLE001
            pass
    try:
        path.unlink()
    except OSError:
        pass
    return f"上次异常退出，已把主音量还原为 {percent}%"


class VolumeGuard:
    """按 `voice.volume_mode` 决定要不要动音量，以及动哪一层。

    三档的含义见模块头注释。无论哪档都不会「压低」用户本来就很大的音量；
    master 档只在当前值低于保底值（或静音）时动手，并记住原值用于还原。
    """

    def __init__(
        self,
        ensure_percent: float = 0.0,
        restore: bool = True,
        mode: str = "",
        backup_path: Optional[Path] = None,
    ):
        if isinstance(mode, bool):
            self.mode = "master" if mode else "off"
        else:
            self.mode = mode if mode in MODES else resolve_mode(
                {"ensure_system_volume": ensure_percent})
        self.ensure_percent = float(ensure_percent)
        self.restore_enabled = bool(restore)
        self.backup_path = Path(backup_path) if backup_path else None
        self.snapshot: Optional[dict] = None
        self.changed = False

    def apply(self) -> str:
        """返回一句人话，说明做了什么（或为什么没做）。"""
        if self.mode == "off":
            return "未启用（不碰系统音量）"
        if not available():
            return "跳过：未安装 pycaw"

        if self.mode == "session":
            changed = set_app_session_percent(os.getpid(), 100.0)
            if changed:
                return f"本程序音轨已设 100%（{changed} 条会话，不动系统音量）"
            return "本程序暂无音频会话，播报时会再试（不动系统音量）"

        if self.ensure_percent <= 0:
            return "未启用（volume_mode = master 但 ensure_system_volume = 0）"

        master = get_master_percent()
        muted = is_muted()
        if master is None:
            return "跳过：读不到主音量"

        self.snapshot = {"percent": master, "muted": bool(muted)}
        if master >= self.ensure_percent and not muted:
            return f"主音量 {master}% 已达标，未改动"

        if self.backup_path is not None:
            write_backup(self.backup_path, master, bool(muted), os.getpid())
        if not set_master_percent(self.ensure_percent):
            return "设置失败（可能被系统策略或独占模式拦住）"

        self.changed = True
        before = f"{master}%" + ("（静音）" if muted else "")
        tail = "，退出时还原" if self.restore_enabled else ""
        return f"主音量 {before} → {self.ensure_percent}%{tail}"

    def restore(self) -> str:
        """还原成监控启动前的音量（只有 master 档改过才需要）。"""
        if self.mode != "master" or not self.changed or self.snapshot is None:
            return ""
        if not self.restore_enabled:
            return f"保持 {self.ensure_percent}%（voice.restore_system_volume = false）"
        pct = self.snapshot["percent"]
        if set_master_percent(pct, force_unmute=False):
            if self.snapshot.get("muted"):
                try:
                    _endpoint().SetMute(True, None)
                except Exception:  # noqa: BLE001
                    pass
            if self.backup_path is not None:
                try:
                    self.backup_path.unlink()
                except OSError:
                    pass
            return f"主音量已还原为 {pct}%"
        return "还原失败"
