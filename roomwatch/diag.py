"""诊断包：把排障需要的文件收成一个 zip，并脱敏。

刻意不收录 `data/`：抓拍、录像、人脸模板都属于隐私，诊断包只回答
「进程为什么退出、配置哪里不对、环境是什么」。
"""

from __future__ import annotations

import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import paths

__all__ = ["mask_value", "mask_config", "build_bundle"]

# 键名里出现这些片段的字符串值一律脱敏
_SECRET_PARTS = (
    "token", "secret", "key", "webhook", "password", "uid",
    "heartbeat_url", "sendkey",
)

_STATE_FILES = (
    "var/state/health.json",
    "var/state/status.json",
    "var/state/last_exit.json",
    "var/state/guardian_state.json",
    "var/state/guardian_history.jsonl",
    "var/state/desired_state.json",
    "var/state/gallery.pid",
)

_LOG_TAIL_BYTES = 2 * 1024 * 1024


def mask_value(value: str) -> str:
    text = str(value)
    if len(text) <= 4:
        return "***"
    return "***" + text[-4:]


def mask_config(data: dict) -> dict:
    """递归复制配置；只替换敏感键的字符串值。"""
    out: dict = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            out[key] = mask_config(value)
        elif isinstance(value, str) and any(part in key.lower() for part in _SECRET_PARTS):
            out[key] = mask_value(value)
        else:
            out[key] = value
    return out


def _output_dir(root: Path, out_dir: Optional[str | Path]) -> Path:
    if out_dir is not None:
        return Path(out_dir)
    if root.resolve() == paths.PROJECT_ROOT.resolve():
        return paths.DIAG_DIR
    return root / "var" / "diag"


def _doctor_text(root: Path) -> str:
    try:
        from . import cli

        checks = cli.run_checks(project_root=root)
        return "\n".join(
            f"[{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in checks
        )
    except Exception as exc:  # noqa: BLE001 - 诊断包本身不能因此失败
        return f"doctor 失败: {exc}"


def _system_text(root: Path) -> str:
    from . import __version__

    return "\n".join([
        f"room-watch: {__version__}",
        f"platform: {platform.platform()}",
        f"python: {sys.version}",
        f"executable: {sys.executable}",
        f"root: {root}",
        f"cwd: {Path.cwd()}",
    ])


def build_bundle(root: str | Path, out_dir: Optional[str | Path] = None) -> Path:
    root = Path(root)
    target_dir = _output_dir(root, out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = target_dir / f"room-watch-diag-{stamp}.zip"

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for log in sorted((root / "var" / "logs").glob("*.log")):
            try:
                data = log.read_bytes()[-_LOG_TAIL_BYTES:]
            except OSError:
                continue
            zf.writestr(log.relative_to(root).as_posix(), data)

        for rel in _STATE_FILES:
            path = root / rel
            if path.is_file():
                try:
                    zf.writestr(rel, path.read_bytes())
                except OSError:
                    continue

        config = root / "config.yaml"
        if config.is_file():
            zf.writestr("config.yaml", config.read_bytes())

        local = root / "config.local.yaml"
        if local.is_file():
            try:
                data = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
                masked = mask_config(data if isinstance(data, dict) else {})
                zf.writestr(
                    "config.local.yaml",
                    yaml.safe_dump(masked, allow_unicode=True, sort_keys=False),
                )
            except (OSError, yaml.YAMLError) as exc:
                zf.writestr("config.local.yaml", f"（读取失败，已跳过：{exc}）\n")

        zf.writestr("doctor.txt", _doctor_text(root) + "\n")
        zf.writestr("system.txt", _system_text(root) + "\n")

    return bundle
