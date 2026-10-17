"""系统输出音量：查与调。

## 为什么监控程序要管系统音量

把音频在代码里做到满刻度（-1 dBFS）只解决了一半问题。实测发现一个更
隐蔽的天花板：**Windows 主音量只有 10%**。

    代码侧：-11.8 dBFS  ──处理──>  -1.0 dBFS      （+10.8 dB）
    系统侧：主音量 10% = 衰减约 20 dB

也就是说，无论音频做得多满，下游都会被砍掉 20 dB。这解释了为什么
"能想到的参数都调到最大了，声音还是小"——问题不在程序里。

所以本模块做三件事：
1. `describe()`  把主音量、静音状态、每个输出设备、应用级音量全列出来
2. `set_master_percent()` 能改主音量
3. `VolumeGuard` 监控运行时把主音量保底到设定值，退出时还原成原样

第 3 条的"还原"很重要：偷偷改掉用户的全局音量而不还原，是种越界。
（进程被强杀时不会还原，这一点在 README 里写明了。）

Windows 专有；非 Windows 或没装 pycaw 时所有函数安全降级为"不可用"。
"""

from __future__ import annotations

import logging
import math
from typing import List, Optional

log = logging.getLogger("room-watch")

DEFAULT_ENSURE = 70.0


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


def list_devices(only_active: bool = True) -> List[dict]:
    """列出**输出**设备及其音量。

    只保留输出设备（id 形如 `{0.0.0.…}`）：这份列表是用来排查
    「声音到底去哪了」的，塞一堆麦克风只会淹没有用信息。

    顺带能发现两个经典事故：默认设备是个没接音箱的 HDMI 显示器，
    或者默认设备被系统禁用了。
    """
    out: List[dict] = []
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


def list_app_sessions() -> List[dict]:
    """各应用自己的音量条（Windows 音量合成器里那些）。

    python.exe 的那一条如果被调低或被静音，我们播什么都是白播。
    """
    out: List[dict] = []
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


class VolumeGuard:
    """监控运行期间把主音量保底到设定值，退出时还原。

    只在「当前音量低于保底值」时动手，不会去压低本来就很大的音量。
    """

    def __init__(self, ensure_percent: float = DEFAULT_ENSURE, restore: bool = True):
        self.ensure_percent = float(ensure_percent)
        self.restore_enabled = bool(restore)
        self.snapshot: Optional[dict] = None
        self.changed = False

    def apply(self) -> str:
        """返回一句人话，说明做了什么（或为什么没做）。"""
        if self.ensure_percent <= 0:
            return "未启用（voice.ensure_system_volume = 0）"
        if not available():
            return "跳过：未安装 pycaw"

        master = get_master_percent()
        muted = is_muted()
        if master is None:
            return "跳过：读不到主音量"

        self.snapshot = {"percent": master, "muted": bool(muted)}

        if master >= self.ensure_percent and not muted:
            return f"主音量 {master}% 已达标，未改动"

        if not set_master_percent(self.ensure_percent):
            return "设置失败（可能被系统策略或独占模式拦住）"

        self.changed = True
        before = f"{master}%" + ("（静音）" if muted else "")
        tail = "，退出时还原" if self.restore_enabled else ""
        return f"主音量 {before} → {self.ensure_percent}%{tail}"

    def restore(self) -> str:
        """还原成监控启动前的音量。"""
        if not self.changed or self.snapshot is None:
            return ""
        if not self.restore_enabled:
            return f"保持 {self.ensure_percent}%（voice.restore_system_volume = false）"
        pct = self.snapshot["percent"]
        if set_master_percent(pct, force_unmute=False):
            if self.snapshot.get("muted"):
                try:
                    _endpoint().SetMute(True, None)
                except Exception:
                    pass
            return f"主音量已还原为 {pct}%"
        return "还原失败"
