"""视频编码后端：ffmpeg 编码器探测、可用性验证与选择。

为什么不是「看配置写谁就用谁」
------------------------------
`ffmpeg -encoders` 里列出 h264_nvenc，只说明编码器编译进了 ffmpeg，
不代表这台机器能用：驱动被拒、Windows N 版缺 Media Foundation、
RDP 会话里硬件编码不可用都会让进程当场失败。所以选择分两步：
先按名字排除明显不存在的候选，再真的编 3 帧 256×144 验证一次，
第一个成功的才写进缓存。

本模块只负责「用什么编码」；什么时候开录、队列、预录仍由 recorder.py 管。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from typing import Callable, Optional

import cv2
import numpy as np

# auto 只考虑硬件编码。libx264 是软件编码，没有基准证明不比 avc1 慢，
# 需要时由用户显式配置 record.ffmpeg_encoder: libx264。
AUTO_CANDIDATES = ("h264_mf", "h264_nvenc")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_TIMEOUT = 15
_VALIDATE_TIMEOUT = 30
# NVENC 会拒绝 64×64（Invalid argument），256×144 是实测能通过的最小常用尺寸。
_VALIDATE_SIZE = (256, 144)


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
    width, height = _VALIDATE_SIZE
    frames = b"\x00" * (width * height * 3 * 3)  # 3 帧黑画面
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", "10", "-i", "-",
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


def _probe_playable(path: str) -> bool:
    """能不能真的读出一帧。0 帧或只有文件头的小文件会在这里被拦下。"""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return False
        ok, frame = cap.read()
        return bool(ok and frame is not None and frame.size > 0)
    except Exception:  # noqa: BLE001 - 解码器异常一律当作不可播
        return False
    finally:
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass


def _default_spawn(cmd: list[str]):
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)


class FfmpegWriter:
    """把帧从 stdin 喂给 ffmpeg，写出一个标准 H.264 mp4。

    ffmpeg 的 stderr 必须持续读走：管道写满后 ffmpeg 会卡住，收尾时
    wait() 永远等不到退出。读线程只保留最后 20 行用于报错。
    """

    def __init__(
        self,
        ffmpeg: str,
        encoder: str,
        path: str,
        size: tuple,
        fps: float,
        *,
        timeout: float = 8.0,
        spawn: Optional[Callable] = None,
        probe: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.encoder = encoder
        self.path = path
        self.size = (int(size[0]), int(size[1]))
        self.fps = float(fps)
        self.timeout = float(timeout)
        self._spawn = spawn or _default_spawn
        self._probe = probe or _probe_playable
        self.proc = None
        self.error: Optional[str] = None
        self.written = 0
        self._stderr_lines: list[str] = []
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def description(self) -> str:
        return self.encoder

    def open(self) -> bool:
        w, h = self.size
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", str(self.fps), "-i", "-",
            "-c:v", self.encoder, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-y", self.path,
        ]
        try:
            self.proc = self._spawn(cmd)
        except Exception as exc:  # noqa: BLE001 - 找不到 ffmpeg / 权限问题都在这里
            self.error = f"启动 ffmpeg 失败: {exc}"
            return False
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="ffmpeg-stderr",
        )
        self._stderr_thread.start()
        return True

    def _drain_stderr(self) -> None:
        stderr = getattr(self.proc, "stderr", None)
        if stderr is None:
            return
        try:
            for line in iter(stderr.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    del self._stderr_lines[:-20]
        except Exception:  # noqa: BLE001 - 进程退出时管道关闭是正常现象
            pass

    def write(self, frame) -> None:
        if self.proc is None:
            raise RuntimeError("ffmpeg 未启动")
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError) as exc:
            self.error = f"写 ffmpeg 失败: {exc}"
            raise RuntimeError(self.error) from exc
        self.written += 1

    def close(self) -> bool:
        proc = self.proc
        if proc is None:
            return False
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - 进程已经死了，stdin 可能已断开
            pass

        timed_out = False
        try:
            rc = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                rc = proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                rc = -1

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:  # noqa: BLE001 - 已关闭/已被回收都无所谓
            pass

        exists = os.path.exists(self.path)
        size = os.path.getsize(self.path) if exists else 0
        playable = False
        if (not timed_out) and rc == 0 and exists and size > 0 and self.written > 0:
            playable = bool(self._probe(self.path))
        ok = (not timed_out) and rc == 0 and exists and size > 0 and self.written > 0 and playable
        if not ok:
            if timed_out:
                self.error = "ffmpeg 未在超时内退出"
            elif rc != 0:
                detail = " / ".join(self._stderr_lines[-3:])
                self.error = f"ffmpeg 返回 {rc}" + (f": {detail}" if detail else "")
            elif not exists:
                self.error = "ffmpeg 没有写出文件"
            elif self.written <= 0:
                self.error = "ffmpeg 没有写出任何帧"
            elif not playable:
                self.error = "ffmpeg 写出的文件无法播放"
            else:
                self.error = "ffmpeg 写出的是空文件"
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.proc = None
        return ok

    def abort(self) -> None:
        """停止路径用：不等待队列，直接结束进程并删掉半成品。"""
        proc = self.proc
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            try:
                if proc.stderr is not None:
                    proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None
        try:
            os.remove(self.path)
        except OSError:
            pass


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
        self._available: Optional[set[str]] = None
        self._validated: dict[str, bool] = {}

    def _probe_available(self) -> set[str]:
        if self._available is None:
            try:
                self._available = set(self._probe(self.ffmpeg))
            except Exception:  # noqa: BLE001 - ffmpeg 不存在/超时都回退 cv2
                self._available = set()
        return self._available

    def choose(self, exclude=()) -> Optional[str]:
        """返回可用的 ffmpeg 编码器名；None = 用 cv2。

        exclude 里的名字会被跳过：一段录像用某个编码器失败后，
        下一段换下一个候选，而不是反复撞同一个。
        """
        if self.mode == "cv2":
            return None
        available = self._probe_available()
        candidates: list[str] = []
        if self.requested not in ("", "auto"):
            candidates.append(self.requested)
        for name in AUTO_CANDIDATES:
            if name not in candidates:
                candidates.append(name)

        for name in candidates:
            if name in exclude or name not in available:
                continue
            if name not in self._validated:
                try:
                    self._validated[name] = bool(self._validate(self.ffmpeg, name))
                except Exception:  # noqa: BLE001 - 单个候选失败不影响后面的候选
                    self._validated[name] = False
            if self._validated[name]:
                return name
        return None


# 依次尝试。avc1 = H.264，浏览器 <video> 能直接播，是相册页唯一有意义的选项。
# mp4v 兜底能写出来，但网页基本放不了，只在 H.264 不可用时保个「至少有文件」。
CODEC_CHAIN = [
    ("avc1", "H.264（浏览器可直接播放）"),
    ("mp4v", "MPEG-4 Part 2（兜底，浏览器通常放不了）"),
    ("MJPG", "Motion JPEG（兜底，体积大）"),
]


def _open_cv2_writer(path: str, fourcc: str, fps: float, size: tuple):
    """开一个 cv2.VideoWriter。失败返回 None，不抛异常。"""
    try:
        w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, size, True)
        if w.isOpened():
            return w
        w.release()
    except Exception:  # noqa: BLE001 - 编码器报错形式多样，一律当失败处理
        pass
    return None


class Cv2Writer:
    """现有 OpenCV 写出路径，保持 avc1 → mp4v → MJPG 的兜底顺序。"""

    def __init__(self, path: str, size: tuple, fps: float, codecs: Optional[list] = None):
        self._original_path = path
        self.path = path
        self.size = (int(size[0]), int(size[1]))
        self.fps = float(fps)
        self.requested = [c for c in (codecs or ["avc1"]) if c]
        self.description = ""
        self.error: Optional[str] = None
        self._writer = None

    def open(self) -> bool:
        root, ext = os.path.splitext(self._original_path)
        target = self._original_path if ext.lower() in (".mp4", ".avi") else root + ".mp4"
        tried = []
        for name in self.requested + [c for c, _ in CODEC_CHAIN]:
            if name in tried:
                continue
            tried.append(name)
            writer = _open_cv2_writer(target, name, self.fps, self.size)
            if writer is not None:
                self._writer = writer
                self.description = name
                self.path = target
                return True
        return False

    def write(self, frame) -> None:
        if self._writer is None:
            raise RuntimeError("cv2 writer 未打开")
        self._writer.write(frame)

    def close(self) -> bool:
        if self._writer is None:
            return False
        try:
            self._writer.release()
        except Exception:  # noqa: BLE001 - 释放失败也按失败处理，文件可能不完整
            pass
        self._writer = None
        exists = os.path.exists(self.path)
        size = os.path.getsize(self.path) if exists else 0
        if not exists or size <= 0:
            self.error = "cv2 写出的是空文件"
            try:
                os.remove(self.path)
            except OSError:
                pass
            return False
        if not _probe_playable(self.path):
            self.error = "cv2 写出的文件无法播放"
            try:
                os.remove(self.path)
            except OSError:
                pass
            return False
        return True

    def abort(self) -> None:
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        try:
            os.remove(self.path)
        except OSError:
            pass


class WriterFactory:
    """按配置挑一条写出路径；ffmpeg 失败就回退 cv2，连续失败后熔断。"""

    def __init__(
        self,
        cfg: dict,
        *,
        selector: Optional[EncoderSelector] = None,
        cv2_factory: Callable = Cv2Writer,
        ffmpeg_factory: Callable = FfmpegWriter,
    ) -> None:
        cfg = cfg or {}
        self.mode = str(cfg.get("encoder", "auto")).strip().lower()
        self.codec_preference = [
            c.strip() for c in str(cfg.get("codec", "avc1")).split(",") if c.strip()
        ]
        audio = cfg.get("audio") or {}
        self.ffmpeg = str(audio.get("ffmpeg") or "ffmpeg")
        self._selector = selector or EncoderSelector(cfg)
        self._cv2_factory = cv2_factory
        self._ffmpeg_factory = ffmpeg_factory
        self._failures = 0
        self._ffmpeg_off = False
        self._rejected: set[str] = set()
        self._last_encoder: Optional[str] = None
        self._last_used_ffmpeg = False
        self.disabled_reason: Optional[str] = None

    def open(self, path: str, size: tuple, fps: float):
        if self.mode != "cv2" and not self._ffmpeg_off:
            try:
                encoder = self._selector.choose(exclude=self._rejected)
            except Exception:  # noqa: BLE001 - 选择器故障按「没有 ffmpeg」处理
                encoder = None
            if encoder:
                self._last_encoder = encoder
                writer = self._ffmpeg_factory(self.ffmpeg, encoder, path, size, fps)
                if writer.open():
                    self._last_used_ffmpeg = True
                    return writer
                self.note_segment_failed(writer.error or "ffmpeg 打开失败")
            else:
                self.disabled_reason = "没有可用的 ffmpeg 编码器"

        self._last_used_ffmpeg = False
        writer = self._cv2_factory(path, size, fps, self.codec_preference)
        return writer if writer.open() else None

    def note_segment_failed(self, reason: str = "") -> None:
        """一段录像失败后记账；下一段跳过这个编码器，连续两次后熔断。"""
        if self._last_encoder:
            self._rejected.add(self._last_encoder)
        self._failures += 1
        self.disabled_reason = reason or "ffmpeg 失败"
        if self._failures >= 2:
            self._ffmpeg_off = True
            self.disabled_reason = "ffmpeg 连续失败"

    def note_segment_ok(self) -> None:
        """成功一段就把「连续失败」计数清零，避免偶发失败累积成熔断。

        只有这一段真的用了 ffmpeg 才清掉回退原因；cv2 成功不代表 ffmpeg 可用。
        """
        self._failures = 0
        if self._last_used_ffmpeg and not self._ffmpeg_off:
            self.disabled_reason = None
