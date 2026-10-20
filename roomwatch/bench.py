"""采样运行中监控的健康快照，回答「现在到底占多少 CPU / 内存」。

为什么不自己起一个 watcher
--------------------------
摄像头是独占的：基准工具再去开一份，会和正在跑的监控抢设备。所以这里只读
`var/state/health.json`——watcher 每秒写一次，足够回答 CPU 均值/峰值、RSS、
丢帧与心跳是否新鲜。推理延迟的 p50/p95 由 `rw bench-detect` 提供。
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from typing import Callable, Optional, Sequence

from .paths import resolve

DEFAULT_SECONDS = 60.0
DEFAULT_INTERVAL = 1.0


def read_json(path: str) -> dict:
    """读一份 JSON；文件不存在、正在被写、内容损坏都返回空 dict。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summarize_samples(samples: list[dict]) -> dict:
    """把多次健康快照汇总成一行报告。空样本返回全零，不抛异常。"""
    count = len(samples)
    if not count:
        return {
            "count": 0,
            "cpu_mean": 0.0,
            "cpu_peak": 0.0,
            "rss_mean_mb": 0.0,
            "rss_peak_mb": 0.0,
            "stale_samples": 0,
            "errors_peak": 0,
            "frames_latest": 0,
        }
    cpus = [float(s.get("cpu_pct") or 0.0) for s in samples]
    rss = [float(s.get("rss_mb") or 0.0) for s in samples]
    stale = sum(
        1 for s in samples
        if s.get("stale") or float(s.get("_age_seconds") or 0.0) > 5.0
    )
    errors_peak = max((int(s.get("consecutive_errors") or 0) for s in samples),
                      default=0)
    return {
        "count": count,
        "cpu_mean": round(sum(cpus) / count, 1),
        "cpu_peak": round(max(cpus), 1),
        "rss_mean_mb": round(sum(rss) / count, 1),
        "rss_peak_mb": round(max(rss), 1),
        "stale_samples": stale,
        "errors_peak": errors_peak,
        "frames_latest": int(samples[-1].get("loop_seq") or 0),
    }


def _annotate_age(sample: dict) -> dict:
    """给快照补一个「读到时已经多旧」，用于判断采样期间监控是否卡住。"""
    ts = sample.get("updated_at")
    if ts:
        try:
            sample["_age_seconds"] = (
                datetime.now() - datetime.fromisoformat(str(ts))
            ).total_seconds()
        except (ValueError, TypeError):
            pass
    return sample


def sample_health(
    path: str,
    seconds: float,
    interval: float,
    *,
    reader: Callable[[str], dict] = read_json,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """按 interval 采样，直到累计 seconds；读不到的快照跳过。"""
    samples: list[dict] = []
    started = clock()
    while True:
        if clock() - started >= seconds:
            break
        data = reader(path)
        if data:
            samples.append(_annotate_age(data))
        sleep(interval)
    return samples


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rw bench", description="采样运行中监控的 CPU / RSS（不占摄像头）"
    )
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--health", default=None, help="健康快照路径（默认 var/state/health.json）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv or []))

    path = args.health or str(resolve("var/state/health.json"))
    interval = max(0.05, float(args.interval))
    samples = sample_health(path, max(0.0, float(args.seconds)), interval)
    summary = summarize_samples(samples)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if summary["count"] else 1
    if not summary["count"]:
        print(f"没读到健康快照：{path}")
        return 1
    print(f"采样 {summary['count']} 次（{path}）")
    print(f"CPU 均值 {summary['cpu_mean']}% / 峰值 {summary['cpu_peak']}%")
    print(f"RSS 均值 {summary['rss_mean_mb']} MB / 峰值 {summary['rss_peak_mb']} MB")
    print(f"stale 样本 {summary['stale_samples']} 个；最后 loop_seq {summary['frames_latest']}")
    return 0
