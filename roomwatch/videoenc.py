"""视频编码后端：ffmpeg 编码器探测、可用性验证与选择。

为什么不是「看配置写谁就用谁」
------------------------------
`ffmpeg -encoders` 里列出 h264_nvenc，只说明编码器编译进了 ffmpeg，
不代表这台机器能用：驱动被拒、Windows N 版缺 Media Foundation、
RDP 会话里硬件编码不可用都会让进程当场失败。所以选择分两步：
先按名字排除明显不存在的候选，再真的编 3 帧 64×64 验证一次，
第一个成功的才写进缓存。

本模块只负责「用什么编码」；什么时候开录、队列、预录仍由 recorder.py 管。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Callable, Optional

# auto 只考虑硬件编码。libx264 是软件编码，没有基准证明不比 avc1 慢，
# 需要时由用户显式配置 record.ffmpeg_encoder: libx264。
AUTO_CANDIDATES = ("h264_mf", "h264_nvenc")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_TIMEOUT = 15
_VALIDATE_TIMEOUT = 30


def parse_encoders(text: str) -> set[str]:
    """从 `ffmpeg -encoders` 的输出里取编码器名。

    行格式类似 ` V....D h264_mf  H264 via MediaFoundation ...`；
    表头和 `------` 分隔行没有第二个字段，直接跳过。
    """
    names: set[str] = set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if not name or name.startswith("-") or name == "=":
            continue
        names.add(name)
    return names


def probe_encoders(ffmpeg: str, *, runner: Optional[Callable] = None) -> set[str]:
    """运行 `ffmpeg -encoders` 并返回可用编码器名；找不到 ffmpeg 会抛异常。"""
    runner = runner or subprocess.run
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": _PROBE_TIMEOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    result = runner([ffmpeg, "-hide_banner", "-encoders"], **kwargs)
    return parse_encoders(getattr(result, "stdout", "") or "")


def validate_encoder(
    ffmpeg: str,
    encoder: str,
    *,
    runner: Optional[Callable] = None,
    tmpdir: Optional[str] = None,
) -> bool:
    """真的编 3 帧 64×64 再判断编码器能不能用。

    比 `-encoders` 贵几百毫秒，但只在 watcher 启动后第一次录像前做一次；
    换来的是一台「列着 nvenc 却用不了」的机器不会在真实事件里才发现。
    """
    runner = runner or subprocess.run
    own_tmp = tmpdir is None
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="rw-encoder-")
    path = os.path.join(tmpdir, f"probe-{encoder}.mp4")
    frames = b"\x00" * (64 * 64 * 3 * 3)  # 3 帧黑画面
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", "64x64", "-r", "10", "-i", "-",
        "-frames:v", "3", "-c:v", encoder, "-pix_fmt", "yuv420p",
        "-y", path,
    ]
    kwargs = {"input": frames, "capture_output": True, "timeout": _VALIDATE_TIMEOUT}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        result = runner(cmd, **kwargs)
        ok = (
            int(getattr(result, "returncode", 1)) == 0
            and os.path.exists(path)
            and os.path.getsize(path) > 0
        )
    except Exception:  # noqa: BLE001 - 编码器拒绝、超时、ffmpeg 缺失都算不可用
        ok = False
    finally:
        if own_tmp:
            for name in os.listdir(tmpdir):
                try:
                    os.remove(os.path.join(tmpdir, name))
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
    return ok


class EncoderSelector:
    """决定这一进程用哪个 ffmpeg 编码器；None 表示回到 cv2。"""

    def __init__(
        self,
        cfg: dict,
        *,
        probe: Callable[[str], set[str]] = probe_encoders,
        validate: Callable[[str, str], bool] = validate_encoder,
    ) -> None:
        cfg = cfg or {}
        self.mode = str(cfg.get("encoder", "auto")).strip().lower()
        self.requested = str(cfg.get("ffmpeg_encoder", "auto")).strip().lower()
        audio = cfg.get("audio") or {}
        # config._prefer_bundled_ffmpeg 已把分发包里的 tools\ffmpeg.exe 写到这里。
        self.ffmpeg = str(audio.get("ffmpeg") or "ffmpeg")
        self._probe = probe
        self._validate = validate
        self._done = False
        self._chosen: Optional[str] = None

    def choose(self) -> Optional[str]:
        """返回可用的 ffmpeg 编码器名；None = 用 cv2。只探测一次。"""
        if self._done:
            return self._chosen
        self._done = True
        if self.mode == "cv2":
            return None
        try:
            available = self._probe(self.ffmpeg)
        except Exception:  # noqa: BLE001 - ffmpeg 不存在/超时都回退 cv2
            return None

        candidates: list[str] = []
        if self.requested not in ("", "auto"):
            candidates.append(self.requested)
        for name in AUTO_CANDIDATES:
            if name not in candidates:
                candidates.append(name)

        for name in candidates:
            if name not in available:
                continue
            try:
                if self._validate(self.ffmpeg, name):
                    self._chosen = name
                    return name
            except Exception:  # noqa: BLE001 - 单个候选失败不影响后面的候选
                continue
        return None
