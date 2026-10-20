"""编码基准：先测量，再决定要不要换编码器。

为什么单独做一个工具
--------------------
录像主链路目前用 OpenCV `VideoWriter`（avc1 → Media Foundation）。硬件编码
（NVENC / Quick Sync / AMF）可能更快，也可能在本机根本不可用；AGENTS.md 的
硬规则是「换引擎/编码器必须先用基准数据证明有收益」。所以这里只做测量：
同样的帧数、同样的分辨率，分别走 cv2 与 ffmpeg 候选编码器，输出 fps、
每帧耗时与每分钟体积。

边界
----
- 不改变 `record.codec`，不自动切换；要不要换由人看过数据后拍板。
- 不依赖真实摄像头：帧是合成出来的（有明显的运动，避免编码器把静止画面
  压得异常小，测出来的体积失真）。
- 测试只覆盖计算/解析/失败隔离；真实编码属于 `rw bench-encode` 的运行行为。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple

# 产品实际会尝试的 cv2 编码器，顺序与 videoenc.CODEC_CHAIN 一致。
CV2_CODECS = ("avc1", "mp4v", "MJPG")
# 只评估常见的硬件 H.264 编码器；libx264 是软件基线，由 ffmpeg_candidates 追加。
FFMPEG_HW_CODECS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf")
# 软件编码基线：只用于基准对比，不进自动选择。
FFMPEG_EXTRA_CODECS = ("libx264",)

_MB = 1024 ** 2
_ENCODER_LINE = re.compile(r"^\s*[VAS][A-Z.]{5}\s+(\S+)")


def ffmpeg_candidates(available: set[str]) -> list[str]:
    """要放进基准报告的 ffmpeg 编码器：本机存在的硬件编码器 + libx264。"""
    names = [name for name in FFMPEG_HW_CODECS if name in available]
    names.extend(FFMPEG_EXTRA_CODECS)
    return names


def summarize(
    encoder: str,
    *,
    frames: int,
    seconds: float,
    size_bytes: int,
    kind: str = "cv2",
    ok: bool = True,
    error: str = "",
) -> dict:
    """把「帧数 / 耗时 / 体积」换成可比较的指标。纯计算，不做 I/O。"""
    fps = frames / seconds if seconds > 0 and frames > 0 else 0.0
    ms_per_frame = (seconds * 1000.0 / frames) if seconds > 0 and frames > 0 else 0.0
    mb_per_min = (size_bytes / _MB) / (seconds / 60.0) if seconds > 0 else 0.0
    return {
        "encoder": encoder,
        "kind": kind,
        "ok": bool(ok),
        "fps": round(fps, 1),
        "ms_per_frame": round(ms_per_frame, 1),
        "mb_per_min": round(mb_per_min, 1),
        "file_mb": round(size_bytes / _MB, 3),
        "frames": frames,
        "seconds": round(seconds, 3),
        "error": error,
    }


def parse_encoder_list(text: str) -> set[str]:
    """从 `ffmpeg -encoders` 输出里抠出编码器名。

    行格式：` V....D h264_nvenc  NVIDIA NVENC ...`，第一列是 6 位能力标记。
    """
    names: set[str] = set()
    for line in (text or "").splitlines():
        match = _ENCODER_LINE.match(line)
        if match:
            name = match.group(1)
            if name and name != "=":
                names.add(name)
    return names


def available_hw_encoders(ffmpeg: str, runner: Optional[Callable] = None) -> list[str]:
    """本机 ffmpeg 里真正存在的硬件编码器；ffmpeg 不存在时返回空表。"""
    runner = runner or (
        lambda args: subprocess.run(
            args, capture_output=True, text=True, timeout=15, check=False
        )
    )
    try:
        result = runner([ffmpeg, "-hide_banner", "-encoders"])
    except Exception:  # noqa: BLE001 - 没装/不能执行都按「不可用」处理
        return []
    if getattr(result, "returncode", 0) != 0:
        return []
    found = parse_encoder_list(getattr(result, "stdout", "") or "")
    return [name for name in FFMPEG_HW_CODECS if name in found]


def _synthetic_frames(frames: int, size: Tuple[int, int]) -> Iterable:
    """有明显的运动与文字，避免静止画面被压得过小。"""
    import cv2
    import numpy as np

    width, height = size
    for i in range(frames):
        frame = np.zeros((height, width, 3), np.uint8)
        frame[:] = (30 + (i * 7) % 200, 40, 80)
        cv2.putText(frame, f"frame {i:04d}", (20, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(frame, ((i * 13) % width, height // 3), 25, (0, 180, 255), -1)
        yield frame


def _default_writer_factory(path, codec, fps, size):
    from .videoenc import _open_cv2_writer

    return _open_cv2_writer(str(path), codec, float(fps), size)


def bench_cv2(
    codec: str,
    *,
    frames: int,
    size: Tuple[int, int],
    fps: float,
    tmpdir: Path,
    writer_factory: Optional[Callable] = None,
    frame_factory: Optional[Callable] = None,
) -> dict:
    """用 OpenCV VideoWriter 编 N 帧，返回一项基准结果。异常不外抛。"""
    writer_factory = writer_factory or _default_writer_factory
    frame_factory = frame_factory or (lambda: _synthetic_frames(frames, size))
    ext = ".avi" if codec.upper() == "MJPG" else ".mp4"
    path = Path(tmpdir) / f"bench-{codec}{ext}"
    writer = None
    started = time.perf_counter()
    try:
        writer = writer_factory(path, codec, fps, size)
        if writer is None:
            raise RuntimeError("编码器打不开")
        for frame in frame_factory():
            writer.write(frame)
        writer.release()
        writer = None
        seconds = time.perf_counter() - started
        size_bytes = path.stat().st_size if path.exists() else 0
        return summarize(codec, frames=frames, seconds=seconds,
                         size_bytes=size_bytes)
    except Exception as exc:  # noqa: BLE001 - 一个编码器失败不能影响其他项
        return summarize(codec, frames=frames, seconds=0, size_bytes=0,
                         ok=False, error=str(exc))
    finally:
        if writer is not None:
            try:
                writer.release()
            except Exception:  # noqa: BLE001
                pass


def _default_ffmpeg_runner(cmd: Sequence[str], frames: Iterable, timeout: float):
    """把原始帧喂给 ffmpeg stdin，返回 (returncode, stderr)。"""
    proc = subprocess.Popen(
        list(cmd), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for frame in frames:
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        stderr = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
        rc = proc.wait(timeout=timeout)
        return rc, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def bench_ffmpeg(
    encoder: str,
    *,
    frames: int,
    size: Tuple[int, int],
    fps: float,
    tmpdir: Path,
    ffmpeg: str = "ffmpeg",
    runner: Optional[Callable] = None,
    frame_factory: Optional[Callable] = None,
    timeout: float = 120.0,
) -> dict:
    """用 ffmpeg 的指定编码器编 N 帧。不可用/失败都返回 ok=False。"""
    runner = runner or _default_ffmpeg_runner
    frame_factory = frame_factory or (lambda: _synthetic_frames(frames, size))
    path = Path(tmpdir) / f"bench-{encoder}.mp4"
    width, height = size
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-", "-c:v", encoder, "-pix_fmt", "yuv420p",
        "-y", str(path),
    ]
    started = time.perf_counter()
    try:
        rc, stderr = runner(cmd, frame_factory(), timeout)
        seconds = time.perf_counter() - started
    except FileNotFoundError:
        return summarize(encoder, frames=frames, seconds=0, size_bytes=0,
                         kind="ffmpeg", ok=False, error="找不到 ffmpeg")
    except Exception as exc:  # noqa: BLE001
        return summarize(encoder, frames=frames, seconds=0, size_bytes=0,
                         kind="ffmpeg", ok=False, error=str(exc))

    if rc != 0:
        error = (stderr or "").strip().splitlines()
        return summarize(encoder, frames=frames, seconds=0, size_bytes=0,
                         kind="ffmpeg", ok=False,
                         error=error[-1] if error else f"ffmpeg 返回 {rc}")
    size_bytes = path.stat().st_size if path.exists() else 0
    return summarize(encoder, frames=frames, seconds=seconds,
                     size_bytes=size_bytes, kind="ffmpeg")


def run_all(
    *,
    frames: int = 120,
    size: Tuple[int, int] = (1280, 720),
    fps: float = 10.0,
    tmpdir: Path,
    with_ffmpeg: bool = True,
    ffmpeg: str = "ffmpeg",
    cv2_runner: Optional[Callable] = None,
    ffmpeg_bench: Optional[Callable] = None,
    hw_encoders: Optional[Sequence[str]] = None,
) -> list[dict]:
    """跑完 cv2 候选，再跑本机可用的 ffmpeg 硬件编码器。单项失败不中断。"""
    cv2_runner = cv2_runner or bench_cv2
    ffmpeg_bench = ffmpeg_bench or bench_ffmpeg
    results: list[dict] = []

    for codec in CV2_CODECS:
        try:
            results.append(cv2_runner(
                codec, frames=frames, size=size, fps=fps, tmpdir=tmpdir,
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(summarize(codec, frames=frames, seconds=0,
                                     size_bytes=0, ok=False, error=str(exc)))

    if with_ffmpeg:
        if hw_encoders is not None:
            encoders = list(hw_encoders)
        else:
            encoders = ffmpeg_candidates(set(available_hw_encoders(ffmpeg)))
        for encoder in encoders:
            try:
                results.append(ffmpeg_bench(
                    encoder, ffmpeg=ffmpeg, frames=frames, size=size,
                    fps=fps, tmpdir=tmpdir,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(summarize(
                    encoder, frames=frames, seconds=0, size_bytes=0,
                    kind="ffmpeg", ok=False, error=str(exc),
                ))
    return results


def _parse_size(text: str) -> Tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text or "")
    if not match:
        raise argparse.ArgumentTypeError("尺寸写成 1280x720")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 16 or height < 16:
        raise argparse.ArgumentTypeError("尺寸太小")
    return width, height


def _ffmpeg_from_config(config_path: str) -> str:
    """优先用配置里的 ffmpeg（分发版可能在 tools/ffmpeg.exe）。"""
    try:
        from .config import load_config

        cfg = load_config(config_path)
        value = str(((cfg.get("record") or {}).get("audio") or {}).get("ffmpeg") or "")
        return value or "ffmpeg"
    except Exception:  # noqa: BLE001 - 配置读不到就退回 PATH
        return "ffmpeg"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rw bench-encode",
        description="测量录像编码速度（只测量，不改变默认编码器）",
    )
    parser.add_argument("--frames", type=int, default=120, help="每个编码器编码多少帧")
    parser.add_argument("--size", type=_parse_size, default=(1280, 720),
                        help="分辨率，默认 1280x720")
    parser.add_argument("--fps", type=float, default=10.0, help="写进视频的帧率")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--with-ffmpeg", dest="with_ffmpeg", action="store_true",
                        default=True, help="同时测 ffmpeg 硬件编码器（默认）")
    parser.add_argument("--no-ffmpeg", dest="with_ffmpeg", action="store_false")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(list(argv or []))

    if args.frames < 1:
        parser.error("--frames 至少为 1")
    ffmpeg = args.ffmpeg or _ffmpeg_from_config(args.config)
    with tempfile.TemporaryDirectory(prefix="rw-bench-") as tmp:
        results = run_all(
            frames=args.frames, size=args.size, fps=args.fps,
            tmpdir=Path(tmp), with_ffmpeg=args.with_ffmpeg, ffmpeg=ffmpeg,
        )

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
        return 0 if any(r["ok"] for r in results) else 1

    print(f"编码器基准：{args.size[0]}x{args.size[1]} @ {args.fps:g} fps，"
          f"每个编码器 {args.frames} 帧")
    print(f"{'编码器':<14}{'方式':<8}{'结果':<6}{'fps':>8}{'ms/帧':>9}"
          f"{'MB/分钟':>10}{'文件 MB':>10}")
    for item in results:
        status = "ok" if item["ok"] else "失败"
        print(f"{item['encoder']:<14}{item['kind']:<8}{status:<6}"
              f"{item['fps']:>8.1f}{item['ms_per_frame']:>9.1f}"
              f"{item['mb_per_min']:>10.1f}{item['file_mb']:>10.2f}")
        if not item["ok"] and item["error"]:
            print(f"    ! {item['error']}")
    if not any(item["ok"] for item in results):
        print("没有任何编码器成功；录像会退回默认 avc1 并记录失败。",
              file=sys.stderr)
        return 1
    print("结论只用于评估；默认编码器不变（record.codec）。")
    return 0
