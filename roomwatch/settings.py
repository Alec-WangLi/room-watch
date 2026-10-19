"""界面「设置」的后端：读生效值、写 config.local.yaml、写完复读校验。

为什么不写 config.yaml
----------------------
安装包 `[Files]` 是 `Source: {#SourceDir}\\*` + `ignoreversion`，**升级会整体覆盖
config.yaml**。用户点界面存的东西若写进主配置，下次更新就静默消失——这类
「明明存过又变回去了」的故障极难排查。所以界面写的一律落 `config.local.yaml`
（深合并、升级保留），与 `rw push` / `rw setup` 同一套机制。

为什么界面进程不能直接调这里
----------------------------
打包后的 `room-watch.exe` 里只有 tkinter + Pillow，没有 PyYAML。
所以界面是通过 `rw config` / `rw config-set` 子进程来读写，本模块由主环境执行。

三条硬约束
----------
1. 密钥永不回显：`snapshot()` 里凭据只有 `{set, tail}`，全文不出这个模块。
2. 先全量校验再落盘：只要有一项不合法，整次保存都不写（不能存一半）。
3. 落盘后必须复读：用 `load_config()` 重新算一遍生效值，不一致就报错。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field as dc_field
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict

import yaml

from . import health
from .config import load_config
from .delivery import KNOWN_CHANNELS, enabled_channels
from .heartbeat import load_heartbeat

__all__ = [
    "Field", "SPEC", "LOCAL_NAME", "snapshot", "apply", "coerce",
    "read_local", "write_local", "sections", "unset", "gallery_info",
    "heartbeat_info", "outbox_info", "channel_issues",
]

MAIN_NAME = "config.yaml"
LOCAL_NAME = "config.local.yaml"

LOCAL_HEADER = """# 本机配置覆盖（不入库、不要提交）
# 由界面「设置」/ `rw setup` / `rw push` 生成：只写与本机相关的差异。
# 为什么不写进 config.yaml：升级安装包会整体覆盖主配置，写那里的设置会丢。
# 想改检测灵敏度、录像时长这类参数，请改 config.yaml，不要在这里重复写。
"""

# 判定「占位串」的规则与 setup_push 保持一致：只看 replace_me，不看前缀。
# （Server酱 真 key 就是 SCT 开头，用前缀判定会把真凭据当成占位符。）
PLACEHOLDER = "replace_me"


@dataclass(frozen=True)
class Field:
    """一个可在界面上编辑的配置项。key 是点分路径，落到 YAML 的嵌套结构。"""

    key: str
    label: str
    section: str
    kind: str = "text"          # text | int | float | bool | choice | secret
    choices: tuple = dc_field(default_factory=tuple)
    lo: float = 0.0
    hi: float = 0.0
    secret: bool = False
    hint: str = ""
    min_len: int = 0

    @property
    def path(self) -> tuple:
        return tuple(self.key.split("."))


SPEC: Dict[str, Field] = {}


def _add(item: Field) -> None:
    SPEC[item.key] = item


# ---- 摄像头 ----
_add(Field("camera.index", "本机摄像头编号", "摄像头", "int", lo=0, hi=15,
           hint="0 通常是第一个；不对就试 1、2。改完点「测试摄像头」验证"))
_add(Field("camera.source", "网络摄像头地址", "摄像头", "secret", secret=True,
           hint="rtsp://用户名:密码@IP:554/...；留空＝用本机编号"))

# ---- 推送 ----
_add(Field("notify.channels", "启用的渠道", "推送", "multi", KNOWN_CHANNELS,
           hint="勾选几个就同时发给几个；只选一个时没有冗余，建议至少两个"))
_add(Field("notify.wecom_bot.webhook", "企业微信机器人 Webhook", "推送", "secret", secret=True,
           hint="整条地址直接粘贴；只填 key 也认"))
_add(Field("notify.wecom_bot.send_video", "录像也推送", "推送", "bool",
           hint="收尾后压缩到 18MB 以内再发，压不到就只发文字"))
_add(Field("notify.wxpusher.app_token", "WxPusher appToken", "推送", "secret", secret=True))
_add(Field("notify.wxpusher.uid", "WxPusher UID", "推送", "secret", secret=True))
_add(Field("notify.serverchan.sendkey", "ServerChan SendKey", "推送", "secret", secret=True,
           hint="第三方推送服务 Server酱；只保存在本机"))
_add(Field("notify.webhook.url", "通用 webhook 地址", "推送", "secret", secret=True,
           hint="ntfy / Gotify / Bark / 自建服务；收到 JSON POST"))
_add(Field("notify.webhook.timeout_seconds", "webhook 超时（秒）", "推送", "int", lo=3, hi=120))
_add(Field("notify.webhook.max_image_mb", "webhook 内联图片上限（MB）", "推送", "int",
           lo=0, hi=20, hint="超过就只发文字；0 = 不内联"))

# ---- 相册 ----
_add(Field("gallery.host", "允许局域网访问", "相册", "choice",
           ("127.0.0.1", "0.0.0.0"),
           hint="127.0.0.1＝仅本机；0.0.0.0＝手机/局域网可访问（必须设置密码）"))
_add(Field("gallery.token", "相册访问密码", "相册", min_len=12,
           hint="至少 12 位；手机访问时用户名随意，密码就填这个。这里明文显示，方便输入"))

# ---- 身份 ----
_add(Field("identity.threshold", "相似度阈值", "身份", "float", lo=0.1, hi=0.99,
           hint="越小越容易认成本人；0.45 是保守值"))
_add(Field("identity.unknown_policy", "认不出的人怎么处理", "身份", "choice",
           ("alert", "ignore"), hint="alert=照常告警（安全优先）；ignore=当成本人静默"))
_add(Field("identity.on_owner", "认出本人时怎么做", "身份", "choice", ("skip", "log"),
           hint="skip=不抓拍不录像不推送；log=照录但只写日志"))

# ---- 心跳 ----
_add(Field("monitor.heartbeat_url", "心跳成功 URL", "心跳", "secret", secret=True,
           hint="healthchecks.io 等服务的完整 URL；留空＝不启用外部心跳"))
_add(Field("monitor.heartbeat_fail_url", "心跳失败 URL", "心跳", "secret", secret=True,
           hint="持续不健康时 ping 它；留空＝停止 ping，由外部服务宽限期告警"))
_add(Field("monitor.heartbeat_start_url", "心跳启动 URL", "心跳", "secret", secret=True))
_add(Field("monitor.heartbeat_interval_seconds", "心跳间隔（秒）", "心跳", "int",
           lo=60, hi=86400))
_add(Field("monitor.heartbeat_timeout_seconds", "心跳超时（秒）", "心跳", "int",
           lo=3, hi=60))

# ---- 自启 ----
# 开机自启拉起的是守护进程，所以「它多久确认一次」天然属于这一页。
_add(Field("guardian.check_seconds", "守护检查间隔（秒）", "自启", "int", lo=15, hi=3600,
           hint="开机自启拉起的是守护进程，它每隔这么久确认监控还活着"))

# ---- 声音 ----
_add(Field("voice.enabled", "检测到人时播报语音", "声音", "bool"))
_add(Field("voice.text", "播报内容", "声音", "text"))
_add(Field("voice.cooldown_seconds", "同一人冷却（秒）", "声音", "float", lo=0, hi=3600))
_add(Field("voice.volume_mode", "如何让提示音听得见", "声音", "choice",
           ("off", "session", "master"),
           hint="off=完全不碰系统音量；session=只提本程序音轨；master=保底主音量（会改你的系统音量）"))


def sections() -> list[str]:
    """界面上页签的顺序。"""
    return ["摄像头", "推送", "相册", "身份", "心跳", "自启", "声音"]


# ---------------------------------------------------------------- 值处理


def get_path(data: dict, key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(data: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def del_path(data: dict, key: str) -> bool:
    """删掉一个点分键；父节点空了也一并收掉。返回是否真的删到了。"""
    parts = key.split(".")
    node = data
    parents = []
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return False
        parents.append((node, part))
        node = child
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in reversed(parents):
        if not parent.get(part):
            del parent[part]
    return True


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _lan_addresses() -> list[tuple[str, str]]:
    """网卡名 + IPv4，排除回环、169.254、虚拟网卡与公网地址。

    虚拟网卡（WSL、Hyper-V、FlClash/Tailscale 等）也会列出私有地址，
    但手机通常连不上；列出来只会让人选错。只保留真正接入局域网的那几条。
    """
    virtual_hints = (
        "vethernet", "wsl", "flclash", "clash", "vmware", "virtualbox",
        "tailscale", "zerotier", "loopback", "bluetooth", "tap",
    )
    try:
        import psutil

        stats = psutil.net_if_stats()
        result = []
        for name, addrs in psutil.net_if_addrs().items():
            if not (stats.get(name) and stats[name].isup):
                continue
            if any(hint in name.lower() for hint in virtual_hints):
                continue
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = str(addr.address)
                if ip.startswith(("127.", "169.254.")):
                    continue
                try:
                    if not ip_address(ip).is_private:
                        continue
                except ValueError:
                    continue
                result.append((name, ip))
    except Exception:  # noqa: BLE001 - 网卡枚举失败不该影响设置读取
        return []

    def rank(item: tuple[str, str]) -> tuple:
        ip = item[1]
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        if ip.startswith("172."):
            return (2, ip)
        return (3, ip)

    return sorted(result, key=rank)


def gallery_info(root: Path | str, cfg: dict | None = None) -> dict:
    """相册实际监听地址 + 可用于手机访问的局域网地址。"""
    root = Path(root)
    cfg = cfg or load_config(str(root / MAIN_NAME))
    gallery = cfg.get("gallery") or {}
    host = str(gallery.get("host") or "127.0.0.1")
    token = str(gallery.get("token") or "").strip()

    port = 0
    try:
        state = json.loads((root / "var" / "state" / "gallery.json").read_text(encoding="utf-8"))
        port = int(state.get("port") or 0)
    except (OSError, ValueError, TypeError):
        port = 0
    if not 0 < port < 65536:
        try:
            port = int(gallery.get("port") or 8765)
        except (TypeError, ValueError):
            port = 8765

    lan_enabled = host not in LOOPBACK_HOSTS
    lan_urls = []
    if lan_enabled:
        for interface, address in _lan_addresses():
            lan_urls.append({
                "interface": interface,
                "address": address,
                "url": f"http://{address}:{port}/",
            })
    return {
        "host": host,
        "port": port,
        "token_set": bool(token),
        "lan_enabled": lan_enabled,
        "local_url": f"http://127.0.0.1:{port}/",
        "lan_urls": lan_urls,
    }


def coerce(field: Field, raw: Any) -> Any:
    """把界面上的字符串变成配置里该有的类型。非法值抛 ValueError（中文原因）。"""
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on", "是", "开", "y"):
            return True
        if text in ("0", "false", "no", "off", "否", "关", "n", ""):
            return False
        raise ValueError(f"{field.label}只能是「是」或「否」，收到：{raw}")

    if field.kind in ("int", "float"):
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field.label}要填数字，收到：{raw}") from None
        if field.kind == "int":
            if abs(number - round(number)) > 1e-9:
                raise ValueError(f"{field.label}要是整数，收到：{raw}")
            number = int(round(number))
        if field.hi > field.lo and not (field.lo <= number <= field.hi):
            raise ValueError(f"{field.label}要在 {field.lo}~{field.hi} 之间，收到：{raw}")
        return number

    if field.kind == "choice":
        text = str(raw).strip()
        if text not in field.choices:
            raise ValueError(f"{field.label}只能是 {'/'.join(field.choices)}，收到：{raw}")
        return text

    if field.kind == "multi":
        if isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = str(raw).replace(";", ",").split(",")
        result: list[str] = []
        for item in items:
            name = str(item).strip().lower()
            if not name:
                continue
            if name not in field.choices:
                raise ValueError(f"{field.label}里有不认识的渠道：{name}")
            if name not in result:
                result.append(name)
        return result

    text = str(raw).strip()
    if field.min_len and text and len(text) < field.min_len:
        raise ValueError(f"{field.label}至少 {field.min_len} 位，收到 {len(text)} 位")
    return text


def secret_info(value: Any) -> dict:
    """凭据只回「有没有」和末 4 位，绝不回全文。"""
    text = str(value or "").strip()
    if not text or PLACEHOLDER in text.lower():
        return {"set": False, "tail": ""}
    tail = text[-4:] if len(text) >= 8 else ""
    return {"set": True, "tail": f"…{tail}" if tail else ""}


def channel_issues(cfg: dict) -> dict:
    """启用但凭据不完整的渠道；界面要显式提示，不能让告警安静地发不出去。"""
    notify = cfg.get("notify") or {}
    issues: dict = {}
    for channel in enabled_channels(notify):
        if channel == "wecom_bot":
            value = str((notify.get("wecom_bot") or {}).get("webhook") or "").strip()
            if not value or "REPLACE" in value.upper():
                issues[channel] = "webhook 未配置"
        elif channel == "wxpusher":
            block = notify.get("wxpusher") or {}
            if not str(block.get("app_token") or "").strip() or not str(block.get("uid") or "").strip():
                issues[channel] = "appToken/UID 未配置"
        elif channel == "serverchan":
            value = str((notify.get("serverchan") or {}).get("sendkey") or "").strip()
            if not value or "replace_me" in value.lower():
                issues[channel] = "SendKey 未配置"
        elif channel == "webhook":
            value = str((notify.get("webhook") or {}).get("url") or "").strip()
            if not value or "replace_me" in value.lower():
                issues[channel] = "URL 未配置"
    return issues


def heartbeat_info(root: Path | str, cfg: dict) -> dict:
    hb = load_heartbeat(cfg)
    state = health.read_json(Path(root) / "var" / "state" / "guardian_state.json") or {}
    return {
        "configured": hb.configured,
        "fail_url_set": bool(hb.fail_url),
        "interval_seconds": hb.interval_seconds,
        "last_at": state.get("last_heartbeat_at"),
        "last_ok": state.get("last_heartbeat_ok"),
        "last_error": str(state.get("last_heartbeat_error") or ""),
    }


def outbox_info(root: Path | str, cfg: dict) -> dict:
    """给设置界面看的队列与渠道健康；数据库不存在时返回零值，不主动建库。"""
    root = Path(root)
    outbox_cfg = cfg.get("outbox") or {}
    db = outbox_cfg.get("database")
    if not db:
        storage = cfg.get("storage") or {}
        event_db = storage.get("database")
        db = (str(Path(event_db).with_name("outbox.db")) if event_db
              else str(root / "var" / "db" / "outbox.db"))
    path = Path(str(db))
    if not path.is_absolute():
        path = root / path
    result = {
        "pending": 0,
        "failed": 0,
        "last_success_at": None,
        "channels": {},
    }
    if not path.is_file():
        return result
    try:
        from .outbox import OutboxStore

        store = OutboxStore(path)
        try:
            stats = store.stats()
            result.update({
                "pending": int(stats.get("pending", 0)),
                "failed": int(stats.get("failed_permanent", 0)),
                "last_success_at": stats.get("last_success_at"),
                "channels": store.channel_health(),
            })
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - 读取失败不该让设置窗口打不开
        pass
    return result


def snapshot(root: Path | str) -> dict:
    """界面要显示的全部内容：普通值 + 凭据状态。"""
    root = Path(root)
    cfg = load_config(str(root / MAIN_NAME))
    values: dict = {}
    secrets: dict = {}
    for field in SPEC.values():
        if field.key == "notify.channels":
            values[field.key] = ",".join(
                enabled_channels(cfg.get("notify") or {})
            )
            continue
        raw = get_path(cfg, field.key)
        if field.kind == "choice" and isinstance(raw, bool):
            # YAML 1.1 把裸 off/on 当布尔：界面要看到 "off" 而不是 False
            raw = "on" if raw else "off"
        if field.secret:
            secrets[field.key] = secret_info(raw)
        else:
            values[field.key] = raw
    return {
        "values": values,
        "secrets": secrets,
        "files": {"main": str(root / MAIN_NAME), "local": str(root / LOCAL_NAME)},
        "sections": sections(),
        "gallery": gallery_info(root, cfg),
        "heartbeat": heartbeat_info(root, cfg),
        "outbox": outbox_info(root, cfg),
        "channels": enabled_channels(cfg.get("notify") or {}),
        "channel_issues": channel_issues(cfg),
        "labels": {key: {"label": f.label, "section": f.section, "kind": f.kind,
                         "choices": list(f.choices), "hint": f.hint,
                         "secret": f.secret}
                   for key, f in SPEC.items()},
    }


# ---------------------------------------------------------------- 落盘


def read_local(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_local(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        LOCAL_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def apply(root: Path | str, changes: dict) -> dict:
    """保存一批设置。返回 {ok, applied, errors}。

    任何一项不合法就整体不写：半存比不存更难查（用户看到「部分生效」）。
    """
    root = Path(root)
    errors: dict = {}
    coerced: dict = {}

    for key, raw in (changes or {}).items():
        field = SPEC.get(key)
        if field is None:
            errors[key] = f"不认识的设置项：{key}"
            continue
        if field.secret and not str(raw if raw is not None else "").strip():
            continue          # 密钥留空 = 保持原值（界面上显示的是掩码）
        try:
            coerced[key] = coerce(field, raw)
        except ValueError as exc:
            errors[key] = str(exc)

    if errors:
        return {"ok": False, "applied": [], "errors": errors}
    if not coerced:
        return {"ok": True, "applied": [], "errors": {}}

    # 跨字段校验：允许局域网访问时，生效密码必须够长。放在落盘前，
    # 否则会出现「设置存进去了、相册却拒绝启动」。只在本次确实改了相册设置时
    # 检查，避免一个历史遗留的无效相册配置挡住推送/声音等无关保存。
    if any(key.startswith("gallery.") for key in coerced):
        current = load_config(str(root / MAIN_NAME))
        host = str(coerced.get("gallery.host",
                               get_path(current, "gallery.host")) or "127.0.0.1")
        token = str(coerced.get("gallery.token",
                                get_path(current, "gallery.token")) or "").strip()
        if host not in LOOPBACK_HOSTS and len(token) < 12:
            errors["gallery.token"] = (
                "允许局域网访问时必须设置至少 12 位相册密码（房间画面不能裸奔）"
            )
            return {"ok": False, "applied": [], "errors": errors}

    local_path = root / LOCAL_NAME
    data = read_local(local_path)
    for key, value in coerced.items():
        set_path(data, key, value)
    write_local(local_path, data)

    # 复读：合并后的生效值必须与刚写的完全一致，否则不许报成功
    cfg = load_config(str(root / MAIN_NAME))
    for key, value in coerced.items():
        got = get_path(cfg, key)
        if got != value:
            errors[key] = f"复读不一致：期望 {value!r}，读回 {got!r}（检查 {LOCAL_NAME} 是否可写）"

    return {"ok": not errors, "applied": list(coerced), "errors": errors}


def unset(root: Path | str, keys: list[str]) -> dict:
    """从 config.local.yaml 删除指定键，回落到 config.yaml 的默认值。

    主要给「改回本机摄像头」用：camera.source 是密钥字段，界面留空＝不改，
    所以必须有一个明确的删除动作，否则换不回 USB 摄像头。
    """
    root = Path(root)
    errors: dict = {}
    for key in keys:
        if not key or key.startswith(".") or key.endswith(".") or ".." in key:
            errors[key] = f"键名不合法：{key!r}"
    if errors:
        return {"ok": False, "applied": [], "errors": errors}

    local_path = root / LOCAL_NAME
    data = read_local(local_path)
    removed = [key for key in keys if del_path(data, key)]
    try:
        write_local(local_path, data)
    except OSError as exc:
        return {"ok": False, "applied": [],
                "errors": {keys[0] if keys else "": f"写配置失败：{exc}"}}

    reread = read_local(local_path)
    for key in keys:
        if get_path(reread, key) is not None:
            errors[key] = f"删除后仍能读到 {key}（检查 {LOCAL_NAME} 是否可写）"
    return {"ok": not errors, "applied": removed, "errors": errors}
