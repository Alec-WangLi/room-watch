"""日志小工具：轮转文件 handler 与固定字段事件行。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["rotating_file_handler", "safe_rotating_file_handler", "event"]


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


def safe_rotating_file_handler(
    path: str | Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
) -> logging.Handler | None:
    """打不开日志文件时返回 None，不让程序在启动阶段直接死掉。

    日志目录可能是只读的、被安全软件锁住、或者路径撞上了一个同名文件。
    这些都不该阻止监控运行；最差也就是只剩控制台输出。
    """
    try:
        return rotating_file_handler(path, max_bytes=max_bytes, backups=backups)
    except OSError as exc:
        logging.getLogger("room-watch").warning(
            "日志文件打不开，改为只输出到控制台：%s（%s）", path, exc)
        return None


def event(logger: logging.Logger, name: str, **fields) -> None:
    """输出固定字段的「事件」日志行，便于日志检索与诊断包分析。"""
    body = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("event=%s %s", name, body)
