"""启动自检：配置、模型、目录、推送、磁盘是否真的可用。

这些检查不打开摄像头（摄像头独占，watcher 自己会打开），
但能在启动时把「装是装上了，实际发不出告警」这类问题暴露出来。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = ["CheckResult", "run"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _resolve(root: str | Path, raw) -> Optional[Path]:
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else Path(root) / path


def _check_models(cfg: dict, root: str | Path) -> CheckResult:
    detector = cfg.get("detector") or {}
    face = cfg.get("face") or {}
    identity = cfg.get("identity") or {}

    checked: list[Path] = []
    missing: list[str] = []
    for key in ("model", "onnx_model"):
        path = _resolve(root, detector.get(key))
        if path:
            checked.append(path)
            if not path.is_file():
                missing.append(str(path))
    face_path = _resolve(root, face.get("model"))
    if face_path:
        checked.append(face_path)
        if not face_path.is_file():
            missing.append(str(face_path))

    owner = _resolve(root, identity.get("owner_file"))
    owner_note = "本人未注册（可选）" if owner and not owner.is_file() else ""

    if missing:
        return CheckResult("模型", False, "缺少：" + "、".join(missing))
    detail = f"{len(checked)} 个模型文件齐全" if checked else "未配置模型路径"
    if owner_note:
        detail += "；" + owner_note
    return CheckResult("模型", True, detail)


def _check_dirs(root: str | Path) -> CheckResult:
    targets = [
        Path(root) / "data" / "captures",
        Path(root) / "var" / "state",
        Path(root) / "var" / "logs",
        Path(root) / "var" / "db",
    ]
    bad: list[str] = []
    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".roomwatch-write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{target}: {exc}")
    if bad:
        return CheckResult("目录", False, "不可写：" + "；".join(bad))
    return CheckResult("目录", True, "运行目录可写")


def _is_placeholder(value: str) -> bool:
    return not value or "replace_me" in value.lower()


def _check_push(cfg: dict) -> CheckResult:
    notify = cfg.get("notify") or {}
    channel = str(notify.get("channel") or "none").strip().lower()
    if channel in ("none", ""):
        return CheckResult("推送", False, "未配置推送渠道（告警只会写日志）")

    if channel == "wecom_bot":
        webhook = str((notify.get("wecom_bot") or {}).get("webhook") or "")
        if _is_placeholder(webhook):
            return CheckResult("推送", False, "企业微信 webhook 未配置或仍是占位串")
    elif channel == "wxpusher":
        block = notify.get("wxpusher") or {}
        token = str(block.get("app_token") or "")
        uid = str(block.get("uid") or "")
        if _is_placeholder(token) or _is_placeholder(uid):
            return CheckResult("推送", False, "WxPusher 的 app_token / uid 未配置")
    elif channel == "serverchan":
        key = str((notify.get("serverchan") or {}).get("sendkey") or "")
        if _is_placeholder(key):
            return CheckResult("推送", False, "Server酱 sendkey 未配置")
    else:
        return CheckResult("推送", False, f"未知渠道：{channel}")

    return CheckResult("推送", True, f"渠道 {channel} 已配置")


def _check_disk(
    cfg: dict, root: str | Path, free_bytes: Optional[int]
) -> CheckResult:
    storage = cfg.get("storage") or {}
    try:
        min_gb = float(storage.get("min_free_gb", 3) or 0)
    except (TypeError, ValueError):
        min_gb = 3.0
    try:
        free = int(free_bytes) if free_bytes is not None else shutil.disk_usage(str(root)).free
    except OSError as exc:
        return CheckResult("磁盘", False, f"无法读取磁盘空间：{exc}")
    need = int(min_gb * (1024 ** 3))
    ok = free >= need
    detail = f"剩余 {free / (1024 ** 3):.1f} GB（下限 {min_gb:g} GB）"
    return CheckResult("磁盘", ok, detail)


def run(
    cfg: dict, root: str | Path, *, free_bytes: Optional[int] = None
) -> list[CheckResult]:
    return [
        _check_models(cfg or {}, root),
        _check_dirs(root),
        _check_push(cfg or {}),
        _check_disk(cfg or {}, root, free_bytes),
    ]
