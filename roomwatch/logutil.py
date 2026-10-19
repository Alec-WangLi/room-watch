"""日志小工具：轮转文件 handler 与固定字段事件行。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["rotating_file_handler", "event"]


def rotating_file_handler(
    path: str | Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
) -> logging.Handler:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        target,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )


def event(logger: logging.Logger, name: str, **fields) -> None:
    """输出固定字段的「事件」日志行，便于日志检索与诊断包分析。"""
    body = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("event=%s %s", name, body)
