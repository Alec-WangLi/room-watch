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

import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict

import yaml

from .config import load_config

__all__ = [
    "Field", "SPEC", "LOCAL_NAME", "snapshot", "apply", "coerce",
    "read_local", "write_local", "sections",
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

    @property
    def path(self) -> tuple:
        return tuple(self.key.split("."))


SPEC: Dict[str, Field] = {}


def _add(item: Field) -> None:
    SPEC[item.key] = item


# ---- 推送 ----
_add(Field("notify.channel", "推送渠道", "推送", "choice",
           ("wecom_bot", "wxpusher", "serverchan", "none"),
           hint="wecom_bot 是唯一能把照片直接发到微信的渠道"))
_add(Field("notify.wecom_bot.webhook", "企业微信机器人 Webhook", "推送", "secret", secret=True,
           hint="整条地址直接粘贴；只填 key 也认"))
_add(Field("notify.wecom_bot.send_video", "录像也推送", "推送", "bool",
           hint="收尾后压缩到 18MB 以内再发，压不到就只发文字"))
_add(Field("notify.wxpusher.app_token", "WxPusher appToken", "推送", "secret", secret=True))
_add(Field("notify.wxpusher.uid", "WxPusher UID", "推送", "secret", secret=True))
_add(Field("notify.serverchan.sendkey", "Server酱 SendKey", "推送", "secret", secret=True))

# ---- 身份 ----
_add(Field("identity.threshold", "相似度阈值", "身份", "float", lo=0.1, hi=0.99,
           hint="越小越容易认成本人；0.45 是保守值"))
_add(Field("identity.unknown_policy", "认不出的人怎么处理", "身份", "choice",
           ("alert", "ignore"), hint="alert=照常告警（安全优先）；ignore=当成本人静默"))
_add(Field("identity.on_owner", "认出本人时怎么做", "身份", "choice", ("skip", "log"),
           hint="skip=不抓拍不录像不推送；log=照录但只写日志"))

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
    return ["推送", "身份", "自启", "声音"]


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

    return str(raw).strip()


def secret_info(value: Any) -> dict:
    """凭据只回「有没有」和末 4 位，绝不回全文。"""
    text = str(value or "").strip()
    if not text or PLACEHOLDER in text.lower():
        return {"set": False, "tail": ""}
    tail = text[-4:] if len(text) >= 8 else ""
    return {"set": True, "tail": f"…{tail}" if tail else ""}


def snapshot(root: Path | str) -> dict:
    """界面要显示的全部内容：普通值 + 凭据状态。"""
    root = Path(root)
    cfg = load_config(str(root / MAIN_NAME))
    values: dict = {}
    secrets: dict = {}
    for field in SPEC.values():
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
