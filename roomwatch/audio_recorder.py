"""事件录像的音轨：录麦克风 + 与视频合并。

为什么音频要单独一个模块
------------------------
OpenCV 的 VideoWriter 只会写画面，一行音频都写不了。要声音只能另开一路：
sounddevice 录 WAV，收尾后再用 ffmpeg 把音轨并进 mp4。

必须解决的对齐问题（最容易翻车的点）
------------------------------------
录像带 **预录**（默认 8 秒）：视频开头是「人还没进门」的画面，
但这 8 秒的声音当时并没有录——录音是从「确认有人」那一刻才开始的。
如果直接合并，你听到的说话声会整段晚 8 秒，对不上张嘴的动作。

所以合并时用 ffmpeg 的 `adelay` 给音轨补一段静音，长度 = 实际预录帧数 / fps。
预录帧数由 Recorder.start() 记在 `last_pre_frames` 里，不是拍脑袋用配置值——
摄像头刚启动时缓冲还没攒满，实际预录往往短于配置。

降级
----
以下任一情况都只影响音轨，不影响视频：
- 没装 sounddevice、没有麦克风、麦克风被其他程序独占
- ffmpeg 不在 PATH
失败时保留 mp4（无声）+ 一个 wav，并在日志里说清楚，绝不吞掉录像。
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import wave
from datetime import datetime
from typing import Optional

import numpy as np

log = logging.getLogger("room-watch")


def build_merge_cmd(
    ffmpeg: str, video: str, audio: str, out: str, delay_ms: int = 0
) -> list:
    """拼 ffmpeg 命令。抽成纯函数是为了能单测参数顺序与转义。"""
    cmd = [ffmpeg, "-y", "-i", video, "-i", audio]
    if delay_ms > 0:
        # all=1 表示所有声道统一延迟，避免声道数不同时参数个数对不上
        cmd += ["-af", f"adelay={int(delay_ms)}:all=1"]
    cmd += [
        "-c:v", "copy",      # 视频流不重编码，几十毫秒完成，也不损失画质
        "-c:a", "aac",
        "-b:a", "96k",
        "-shortest",         # 谁先结束就以谁为准，避免尾部出现无声黑屏
        out,
    ]
    return cmd


def pre_roll_delay_ms(pre_frames: int, fps: float) -> int:
    """预录了多少帧 → 音轨该补多少毫秒静音。"""
    if pre_frames <= 0 or fps <= 0:
        return 0
    return int(round(pre_frames / float(fps) * 1000.0))


class AudioRecorder:
    """把麦克风录成 WAV，收尾后并进 mp4。"""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.device = cfg.get("device")
        self.samplerate = int(cfg.get("samplerate", 16000))
        self.channels = max(1, int(cfg.get("channels", 1)))
        self.keep_wav = bool(cfg.get("keep_wav", False))
        self.ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
        self.merge_timeout = float(cfg.get("merge_timeout_seconds", 60))

        self._stream = None
        self._wave = None
        self._queue: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._wav_path: Optional[str] = None

        self.available = False
        self.last_error: Optional[str] = None
        self.dropped = 0
        self.recorded_frames = 0

        if self.enabled:
            self.available = self._probe()

    # ---------- 探测 ----------

    def _probe(self) -> bool:
        """能不能录音。探测失败只是关掉音轨，不影响监控本身。"""
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"未安装 sounddevice: {exc}"
            log.warning("录音不可用（%s），录像将没有声音。pip install sounddevice", self.last_error)
            return False

        try:
            info = sd.query_devices(self.device, kind="input")
            log.info(
                "录音设备: %s（%s Hz，%s 声道）",
                info.get("name", "?"),
                self.samplerate,
                self.channels,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"找不到可用的麦克风: {exc}"
            log.warning("录音不可用（%s），录像将没有声音", self.last_error)
            return False

        if shutil.which(self.ffmpeg) is None and not os.path.exists(self.ffmpeg):
            log.warning(
                "找不到 ffmpeg（%s），录音能录但没法并进 mp4，将保留独立的 .wav",
                self.ffmpeg,
            )
        return True

    @property
    def recording(self) -> bool:
        return self._stream is not None

    # ---------- 生命周期 ----------

    def start(self, wav_path: str) -> bool:
        """开始录音。失败返回 False，调用方照常录像即可。"""
        if not self.enabled or not self.available or self.recording:
            return False
        try:
            import sounddevice as sd

            os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
            self._wave = wave.open(wav_path, "wb")
            self._wave.setnchannels(self.channels)
            self._wave.setsampwidth(2)  # 16 bit
            self._wave.setframerate(self.samplerate)

            self._queue = queue.Queue(maxsize=200)
            self.dropped = 0
            self.recorded_frames = 0
            self._thread = threading.Thread(
                target=self._write_loop, daemon=True, name="audio-writer"
            )
            self._thread.start()

            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                device=self.device,
                dtype="float32",
                blocksize=0,
                callback=self._callback,
            )
            self._stream.start()
            self._wav_path = wav_path
            log.info("开始录音: %s", wav_path)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("录音启动失败（%s），本段视频将没有声音", exc)
            self._cleanup_failed_start()
            return False

    def _cleanup_failed_start(self) -> None:
        self._stream = None
        try:
            if self._queue is not None:
                self._queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._wave is not None:
            try:
                self._wave.close()
            except Exception:  # noqa: BLE001
                pass
            self._wave = None
        self._queue = None

    def _callback(self, indata, frames, time_info, status) -> None:
        """音频线程回调。这里绝不能做 IO，只往队列塞。"""
        if status:
            log.debug("录音状态: %s", status)
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            self.dropped += 1

    def _write_loop(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                self._wave.writeframes(pcm.tobytes())
                self.recorded_frames += int(chunk.shape[0])
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"写音频失败: {exc}"
                log.error("写音频失败: %s", exc)
                break

    def stop(self) -> Optional[str]:
        """停止录音，返回 WAV 路径。"""
        if not self.recording:
            return self._wav_path
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("关闭录音流异常: %s", exc)
        self._stream = None

        try:
            self._queue.put(None)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        if self._wave is not None:
            try:
                self._wave.close()
            except Exception:  # noqa: BLE001
                pass
            self._wave = None

        path = self._wav_path
        self._wav_path = None
        if path and os.path.exists(path):
            secs = self.recorded_frames / float(self.samplerate or 1)
            log.info("录音结束: %s（%.0f 秒%s）", path, secs,
                     f"，丢块 {self.dropped}" if self.dropped else "")
            return path
        return None

    # ---------- 合并 ----------

    def merge_into(self, video_path: str, wav_path: str, delay_ms: int = 0) -> Optional[str]:
        """把音轨并进 mp4。成功返回视频路径，失败返回 None（视频保持原样）。"""
        if not video_path or not wav_path:
            return None
        if not os.path.exists(video_path) or not os.path.exists(wav_path):
            return None

        ffmpeg = shutil.which(self.ffmpeg) or (self.ffmpeg if os.path.exists(self.ffmpeg) else None)
        if ffmpeg is None:
            log.warning("没有 ffmpeg，音轨未合并（wav 保留在 %s）", wav_path)
            return None

        tmp = os.path.splitext(video_path)[0] + ".withaudio.mp4"
        cmd = build_merge_cmd(ffmpeg, video_path, wav_path, tmp, delay_ms)
        try:
            # CREATE_NO_WINDOW：ffmpeg 是控制台程序，不隐藏的话长期运行会闪黑框
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                cmd,
                timeout=self.merge_timeout,
                capture_output=True,
                creationflags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("调用 ffmpeg 失败（%s），音轨未合并", exc)
            return None

        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
            log.warning("音轨合并失败（ffmpeg 返回 %s）：%s", proc.returncode, " / ".join(tail))
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None

        try:
            os.replace(tmp, video_path)
        except OSError as exc:
            log.warning("替换视频文件失败（%s），保留原视频", exc)
            return None

        log.info("音轨已合并: %s（补静音 %d ms）", video_path, delay_ms)
        if not self.keep_wav:
            try:
                os.remove(wav_path)
            except OSError:
                pass
        return video_path

    def merge_async(self, video_path: str, wav_path: str, delay_ms: int = 0) -> None:
        """合并放到后台线程——ffmpeg 要好几百毫秒，不能卡住检测主循环。"""

        def _run() -> None:
            try:
                self.merge_into(video_path, wav_path, delay_ms)
            except Exception as exc:  # noqa: BLE001
                log.warning("后台合并音轨异常: %s", exc)

        threading.Thread(target=_run, daemon=True, name="audio-merge").start()


def wav_duration(path: str) -> float:
    """读 WAV 时长。给自检和测试用。"""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate() or 1
        return wf.getnframes() / float(rate)


def _now_stamp() -> str:
    return datetime.now().strftime("%H%M%S")
