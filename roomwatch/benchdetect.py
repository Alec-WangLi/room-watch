"""推理基准：给检测器计时，先有数据再谈换引擎。

M4 的规则很明确：ONNX Runtime / OpenVINO / DirectML / CUDA 只有在基准数据
证明有收益时才允许切换，并且不能默认捆绑 CUDA。这个工具先补上「基准数据」
这一半：对当前配置的检测器计时，按需对比 torch 兜底，输出每帧耗时与 fps。

它不会改动运行时使用的引擎，也不安装任何依赖。可选引擎是否“可用”只反映
当前解释器里能不能 import；要真正比较，需要先做适配与单独评审。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from typing import Optional, Sequence, Tuple

DEFAULT_SIZE = (1280, 720)
KNOWN_ENGINES = ("cv2", "onnxruntime", "openvino", "torch")


def summarize(
    *,
    frames: int,
    seconds: float,
    engine: str = "",
    detections: int = 0,
    ok: bool = True,
    error: str = "",
    cpu_seconds: float = 0.0,
    p50_ms: float = 0.0,
    p95_ms: float = 0.0,
) -> dict:
    fps = frames / seconds if frames > 0 and seconds > 0 else 0.0
    ms = (seconds * 1000.0 / frames) if frames > 0 and seconds > 0 else 0.0
    cpu_ms = (cpu_seconds * 1000.0 / frames) if frames > 0 else 0.0
    cores = (cpu_seconds / seconds) if seconds > 0 else 0.0
    return {
        "engine": engine,
        "ok": bool(ok),
        "frames": int(frames),
        "seconds": round(seconds, 3),
        "fps": round(fps, 1),
        "ms_per_frame": round(ms, 1),
        "p50_ms": round(float(p50_ms), 1),
        "p95_ms": round(float(p95_ms), 1),
        "cpu_ms_per_frame": round(cpu_ms, 1),
        "cpu_cores": round(cores, 2),
        "detections": int(detections),
        "error": error,
    }


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (max(0.0, min(100.0, float(p))) / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _iou(a, b) -> float:
    x1 = max(a.x1, b.x1)
    y1 = max(a.y1, b.y1)
    x2 = min(a.x2, b.x2)
    y2 = min(a.y2, b.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a.x2 - a.x1) * max(0.0, a.y2 - a.y1)
    area_b = max(0.0, b.x2 - b.x1) * max(0.0, b.y2 - b.y1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def detections_match(a: list, b: list, *, conf_tol: float = 0.02,
                     iou_tol: float = 0.95) -> bool:
    """两套后端在同一张图上的检测结果是否一致（贪心配对）。"""
    if len(a) != len(b):
        return False
    remaining = list(b)
    for da in a:
        best = None
        best_iou = -1.0
        for i, db in enumerate(remaining):
            iou = _iou(da, db)
            if iou >= iou_tol and abs(da.conf - db.conf) <= conf_tol and iou > best_iou:
                best = i
                best_iou = iou
        if best is None:
            return False
        remaining.pop(best)
    return True


def engine_setting(name: str) -> Optional[str]:
    """把基准命令里的引擎名翻译成 `detector.engine` 的取值。

    `cv2` 是给人的叫法，底层仍是现有的 `onnx`（cv2.dnn）实现。
    """
    key = str(name or "auto").strip().lower()
    if key in ("", "auto"):
        return None
    if key == "cv2":
        return "onnx"
    if key in ("onnx", "onnxruntime", "torch"):
        return key
    raise ValueError(f"未知推理引擎: {name}")


def available_engines() -> list[str]:
    """当前解释器里能 import 的推理后端。cv2 是产品默认，永远在。"""
    available = ["cv2"]
    for name in ("onnxruntime", "openvino", "torch"):
        try:
            if importlib.util.find_spec(name) is not None:
                available.append(name)
        except (ImportError, ValueError):
            continue
    return available


def make_frame(size: Tuple[int, int]):
    """合成一帧有人形的生活场景。检测结果不重要，计时要的是真实的前处理量。"""
    import cv2
    import numpy as np

    width, height = size
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = (40, 52, 68)
    # 一点纹理，避免某些后端对纯色画布走极端优化
    for y in range(0, height, 64):
        cv2.line(frame, (0, y), (width, y), (55, 66, 82), 1)
    x = width // 3
    cv2.rectangle(frame, (x, height // 5), (x + width // 5, height * 4 // 5),
                  (170, 190, 210), -1)
    cv2.circle(frame, (x + width // 10, height // 5 + 18), 18, (200, 210, 220), -1)
    cv2.putText(frame, "benchmark frame", (20, height - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 230, 230), 2, cv2.LINE_AA)
    return frame


def bench_detector(
    detector,
    *,
    frames: int = 30,
    size: Tuple[int, int] = DEFAULT_SIZE,
    warmup: int = 3,
    frame=None,
) -> dict:
    """对检测器计时。预热帧不计入结果；异常不外抛。"""
    engine = str(getattr(detector, "name", "?"))
    if frame is None:
        frame = make_frame(size)
    try:
        for _ in range(max(0, int(warmup))):
            detector.detect(frame)
        started = time.perf_counter()
        cpu_started = time.process_time()
        detections = 0
        timings: list[float] = []
        for _ in range(max(1, int(frames))):
            frame_started = time.perf_counter()
            detections += len(detector.detect(frame))
            timings.append((time.perf_counter() - frame_started) * 1000.0)
        seconds = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_started
        return summarize(frames=frames, seconds=seconds, engine=engine,
                         detections=detections, cpu_seconds=cpu_seconds,
                         p50_ms=percentile(timings, 50),
                         p95_ms=percentile(timings, 95))
    except Exception as exc:  # noqa: BLE001 - 一个引擎失败不能拖垮基准
        return summarize(frames=frames, seconds=0, engine=engine,
                         ok=False, error=str(exc))


def run_all(
    config_path: str = "config.yaml",
    *,
    frames: int = 30,
    size: Tuple[int, int] = DEFAULT_SIZE,
    warmup: int = 3,
    source: Optional[str] = None,
    compare: bool = False,
    engines: Sequence[str] = ("auto",),
) -> list[dict]:
    """测当前配置的检测器；compare=True 时再试一次 torch 兜底。"""
    from .config import load_config
    from .onnx_detector import TorchDetector, build_detector
    from .paths import PROJECT_ROOT

    cfg = load_config(config_path)
    det_cfg = dict(cfg.get("detector") or {})
    # 与 watcher.detect() 保持一致：生产环境会把 cv2 线程数压到 detector.threads。
    # 不设这一项会测出「多核全开」的假象，CPU 成本完全不可比。
    try:
        import cv2

        cv2.setNumThreads(max(1, int(det_cfg.get("threads", 2))))
    except Exception:  # noqa: BLE001 - 设不了线程数不影响计时
        pass
    frame = None
    if source:
        import cv2

        frame = cv2.imread(source)
        if frame is None:
            return [summarize(frames=frames, seconds=0, ok=False,
                              error=f"读不了图片：{source}")]

    results: list[dict] = []
    for name in engines:
        try:
            setting = engine_setting(name)
        except ValueError as exc:
            results.append(summarize(frames=frames, seconds=0, engine=name,
                                     ok=False, error=str(exc)))
            continue
        detector_cfg = dict(det_cfg)
        if setting:
            detector_cfg["engine"] = setting
        try:
            detector = build_detector(detector_cfg, base_dir=str(PROJECT_ROOT))
            results.append(bench_detector(
                detector, frames=frames, size=size, warmup=warmup, frame=frame,
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(summarize(frames=frames, seconds=0, engine=name,
                                     ok=False,
                                     error=f"检测器初始化失败：{exc}"))

    if compare:
        base = str(PROJECT_ROOT)
        pt_path = det_cfg.get("model") or "yolov8n.pt"
        if not os.path.isabs(pt_path):
            pt_path = os.path.join(base, pt_path)
        try:
            torch_det = TorchDetector(
                pt_path,
                int(det_cfg.get("imgsz", 640)),
                float(det_cfg.get("conf", 0.55)),
                float(det_cfg.get("iou", 0.45)),
                int(det_cfg.get("threads", 2)),
            )
            results.append(bench_detector(
                torch_det, frames=frames, size=size, warmup=warmup, frame=frame,
            ))
        except Exception as exc:  # noqa: BLE001 - 没装 torch 是正常情况
            results.append(summarize(
                frames=frames, seconds=0, engine="torch", ok=False,
                error=f"torch 不可用：{exc}",
            ))
    return results


def _parse_size(text: str) -> Tuple[int, int]:
    import re

    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text or "")
    if not match:
        raise argparse.ArgumentTypeError("尺寸写成 1280x720")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 16 or height < 16:
        raise argparse.ArgumentTypeError("尺寸太小")
    return width, height


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rw bench-detect",
        description="测量人体检测推理速度（只测量，不切换引擎）",
    )
    parser.add_argument("--frames", type=int, default=30, help="计时帧数")
    parser.add_argument("--warmup", type=int, default=3, help="预热帧数（不计入）")
    parser.add_argument("--size", type=_parse_size, default=DEFAULT_SIZE)
    parser.add_argument("--source", default=None,
                        help="用一张真实图片计时（默认合成画面）")
    parser.add_argument("--compare", action="store_true",
                        help="额外测一次 torch 兜底（需要 ultralytics）")
    parser.add_argument("--engine", default="auto",
                        help="auto | cv2 | onnxruntime | torch；逗号分隔可测多个")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(list(argv or []))

    engines = [item.strip() for item in str(args.engine).split(",") if item.strip()]
    results = run_all(
        args.config, frames=args.frames, size=args.size,
        warmup=args.warmup, source=args.source, compare=args.compare,
        engines=engines or ("auto",),
    )

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
        return 0 if any(item["ok"] for item in results) else 1

    print(f"推理基准：{args.size[0]}x{args.size[1]}，"
          f"预热 {args.warmup} 帧，计时 {args.frames} 帧")
    print("可用后端：" + "、".join(available_engines()))
    print(f"{'引擎':<14}{'结果':<6}{'fps':>8}{'ms/帧':>10}"
          f"{'p50':>8}{'p95':>8}{'CPU ms/帧':>11}{'核数':>7}{'检出':>8}")
    for item in results:
        status = "ok" if item["ok"] else "失败"
        print(f"{item['engine']:<14}{status:<6}{item['fps']:>8.1f}"
              f"{item['ms_per_frame']:>10.1f}{item['p50_ms']:>8.1f}"
              f"{item['p95_ms']:>8.1f}{item['cpu_ms_per_frame']:>11.1f}"
              f"{item['cpu_cores']:>7.2f}{item['detections']:>8}")
        if not item["ok"] and item["error"]:
            print(f"    ! {item['error']}")
    print("结论只用于评估；运行时引擎由 detector.engine 决定，本工具不改它。")
    return 0 if any(item["ok"] for item in results) else 1
