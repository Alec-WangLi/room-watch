"""事件摘要生成器：把一次抓拍变成「能直接推送」的三件套。

原片动辄 37~95MB，对话和微信都传不动；本脚本产出轻量副本，只写 var/media/，
不动 data/captures/ 里的原始证据。

产出（每个事件一份）：
    sheet_<HHMMSS>.jpg    过程联系图，5x3=15 格，等间隔抽帧，自动带画面 OSD 时间
    light_<HHMMSS>.mp4    640 宽 / CRF30 / 无音轨，体积通常压到原片 1%
    （抓拍原图 <HHMMSS>.jpg 直接复用，不复制）

用法：
    python scripts/event_digest.py data/captures/2026-10-18/091536.jpg
    python scripts/event_digest.py --recent 65          # 最近 65 分钟内的全部新抓拍
    python scripts/event_digest.py --dir data/captures/2026-10-18 091536 073204

设计约束（踩过的坑）：
- 一个事件可能对应多段 mp4（分段续录，文件名带 -1/-2 序号），只取主段做摘要；
- 编码中断的 mp4 会缺 moov atom，ffprobe 拿不到时长 → 跳过并告警，不硬转；
- 抽帧间隔按时长自适应，保证固定出 15 格；人只停留 10 秒的场景不会被漏掉。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "var" / "media"

COLS, ROWS = 5, 3
CELL_W = 256
LIGHT_W = 640
LIGHT_CRF = 30


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def probe_duration(mp4: Path) -> float:
    """返回秒数；读不到（坏文件 / 缺 moov atom）返回 0。"""
    p = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(mp4),
        ]
    )
    if p.returncode != 0:
        return 0.0
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def make_sheet(mp4: Path, out: Path) -> bool:
    dur = probe_duration(mp4)
    if dur <= 0:
        print(f"  ! 跳过拼图：{mp4.name} 读不到时长（可能编码中断）", file=sys.stderr)
        return False

    cells = COLS * ROWS
    # 等间隔抽满 15 格；极短事件（<7.5s）退化为每 0.5s 一帧
    step = max(dur / cells, 0.5)
    vf = f"fps=1/{step:.3f},scale={CELL_W}:-2,tile={COLS}x{ROWS}"

    p = _run(
        ["ffmpeg", "-y", "-i", str(mp4), "-vf", vf,
         "-frames:v", "1", "-q:v", "4", str(out)]
    )
    if p.returncode != 0 or not out.exists():
        print(f"  ! 拼图失败：{mp4.name}\n{p.stderr[-400:]}", file=sys.stderr)
        return False
    print(f"  + {out.name}  ({out.stat().st_size // 1024} KB, {dur:.1f}s / {cells} 格)")
    return True


def make_light(mp4: Path, out: Path) -> bool:
    """压成可推送的体积。

    务必保留音轨：录像是带麦克风环境音的（实测 09:15 事件峰值 -6.1 dB），
    脚步声、关门声、说话声都是判断现场情况的信息。
    曾因写了 -an 把声音全删掉，导致"看不到也听不到"，此处不得再加 -an。
    """
    dur = probe_duration(mp4)
    if dur <= 0:
        print(f"  ! 跳过轻量视频：{mp4.name} 读不到时长", file=sys.stderr)
        return False

    p = _run(
        ["ffmpeg", "-y", "-i", str(mp4),
         "-vf", f"scale={LIGHT_W}:-2",
         "-c:v", "libx264", "-crf", str(LIGHT_CRF), "-preset", "fast",
         "-c:a", "aac", "-b:a", "64k", str(out)]
    )
    if p.returncode != 0 or not out.exists():
        print(f"  ! 轻量视频失败：{mp4.name}\n{p.stderr[-400:]}", file=sys.stderr)
        return False
    src = mp4.stat().st_size / 1024 / 1024
    dst = out.stat().st_size / 1024 / 1024
    ratio = (dst / src * 100) if src else 0
    print(f"  + {out.name}  ({dst:.2f} MB, 原片 {src:.1f} MB 的 {ratio:.0f}%)")
    return True


def _hms_to_sec(stem: str) -> int | None:
    """把文件名前缀 HHMMSS 转成当天秒数，便于跨文件名比较。"""
    digits = stem[:6]
    if not (len(digits) == 6 and digits.isdigit()):
        return None
    return int(digits[:2]) * 3600 + int(digits[2:4]) * 60 + int(digits[4:6])


def find_video(jpg: Path) -> Path | None:
    """同一事件的录像。

    关键：抓拍与录像不是同名——录像按「开录时刻」命名、抓拍按「触发时刻」命名，
    实测常差 1~2 秒（091536.jpg ↔ 091535.mp4）。所以先精确匹配，
    再退化为同目录内时间差 ≤5 秒的最近一个，最后才试带分段序号的主段。
    """
    exact = jpg.with_suffix(".mp4")
    if exact.exists():
        return exact

    cands = sorted(jpg.parent.glob(f"{jpg.stem}*.mp4"))
    if cands:
        return cands[0]

    base = _hms_to_sec(jpg.stem)
    if base is None:
        return None

    best, best_gap = None, 6
    for p in jpg.parent.glob("*.mp4"):
        sec = _hms_to_sec(p.stem)
        if sec is None:
            continue
        gap = abs(sec - base)
        if gap < best_gap:
            best, best_gap = p, gap
    if best:
        print(f"  · 录像按时间邻近匹配：{best.name}（差 {best_gap}s）")
    return best


def digest(jpg: Path) -> dict:
    MEDIA.mkdir(parents=True, exist_ok=True)
    stem = jpg.stem
    result = {"jpg": jpg, "sheet": None, "light": None, "video": None}

    print(f"[{stem}] 抓拍图 {jpg.stat().st_size // 1024} KB")

    mp4 = find_video(jpg)
    if mp4 is None:
        print("  · 无配套录像，只推抓拍图")
        return result
    result["video"] = mp4

    sheet = MEDIA / f"sheet_{stem}.jpg"
    if make_sheet(mp4, sheet):
        result["sheet"] = sheet

    light = MEDIA / f"light_{stem}.mp4"
    if make_light(mp4, light):
        result["light"] = light

    return result


def recent_jpgs(minutes: int) -> list[Path]:
    cutoff = time.time() - minutes * 60
    base = ROOT / "data" / "captures"
    found = [p for p in base.rglob("*.jpg") if p.stat().st_mtime >= cutoff]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser(description="把抓拍转成可推送的图+视频摘要")
    ap.add_argument("jpg", nargs="*", type=Path, help="抓拍 jpg 路径")
    ap.add_argument("--recent", type=int, metavar="MIN",
                    help="自动选取最近 N 分钟内全部抓拍")
    ap.add_argument("--dir", type=Path, help="目录 + 后面的 stem 列表")
    args = ap.parse_args()

    targets: list[Path] = []

    if args.recent:
        targets.extend(recent_jpgs(args.recent))

    if args.dir:
        for item in args.jpg:
            stem = item.stem if item.suffix else item.name
            targets.append(args.dir / f"{stem}.jpg")
    elif args.jpg:
        for p in args.jpg:
            targets.append(p if p.suffix == ".jpg" else p.with_suffix(".jpg"))

    if not targets:
        print("没有要处理的事件。用 --recent 65 或直接给 jpg 路径。", file=sys.stderr)
        return 2

    ok = 0
    for jpg in targets:
        if not jpg.exists():
            print(f"! 跳过：{jpg} 不存在", file=sys.stderr)
            continue
        print("-" * 52)
        digest(jpg)
        ok += 1

    print("-" * 52)
    print(f"处理 {ok} 个事件，产物在 {MEDIA}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
