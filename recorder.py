"""事件录像：环形预录 + 后台线程写 H.264。

为什么必须单独一个文件、为什么要开线程
----------------------------------------
实测本机把 720p 帧编成 H.264 要 11~18 ms；而 camera.fps=10 意味着每帧只有 100ms 预算，
里面还扣着 YOLO 推理、人脸检测。如果直接在检测主循环里 writer.write()，
编码就把主循环拖慢，掉帧越多、漏检越多——为了录像反而削弱了检测，本末倒置。
所以这里用一个后台写线程，主循环只负责「把帧丢进队列」这种纳秒级操作。

关于 pre-roll（预录）
--------------------
只在触发那一刻开始录的话，视频开头会缺掉「人推门走进来」的瞬间——而那往往是最想看的。
所以主循环平时就持续把最近若干秒的帧攒在环形缓冲里，开录时先把它们倒进视频，
画面就从「他还没进门」开始了。

缓冲为什么不存原始 ndarray：720p 原始帧 2.7MB/帧，10 秒就是 270MB。
所以存 JPEG 编码后的字节（约 100KB/帧），10 秒 10MB 出头，可以接受。
也正因如此这里刻意**不做抽帧取样**：少存一帧，回放时就少一段画面。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from datetime import datetime
from typing import Deque, Optional

import cv2
import numpy as np

log = logging.getLogger("room-watch")

# 依次尝试。avc1 = H.264，浏览器 <video> 能直接播，是相册页唯一有意义的选项。
# mp4v 兜底能写出来，但网页基本放不了，只在 H.264 不可用时保个「至少有文件」。
CODEC_CHAIN = [
    ("avc1", "H.264（浏览器可直接播放）"),
    ("mp4v", "MPEG-4 Part 2（兜底，浏览器通常放不了）"),
    ("MJPG", "Motion JPEG（兜底，体积大）"),
]


def _open_writer(path: str, fourcc: str, fps: float, size: tuple):
    """开一个 VideoWriter。失败返回 None，不抛异常。

    本机实际观察：OpenCV 报 'H264 not supported' 后会自动 fallback 到 avc1，
    同时 stderr 会刷一串 libopenh264 加载失败——那是 FFmpeg 后端在抱怨，
    Media Foundation 后端会接手并成功，那些红字可以忽略。
    """
    try:
        w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, size, True)
        if w.isOpened():
            return w
        w.release()
    except Exception as e:  # noqa: BLE001 - 编码器报错形式多样，一律当失败处理
        log.debug("编码器 %s 打开异常: %s", fourcc, e)
    return None


class PreRollBuffer:
    """环形缓冲：永远留着最近 N 帧，满了就顶掉最旧的。

    存 JPEG 字节而不是 ndarray，见文件头注释里的内存账。
    """

    def __init__(self, seconds: float, fps: float, quality: int = 80):
        self.capacity = max(0, int(round(seconds * fps)))
        self.quality = int(quality)
        self._items: Deque[bytes] = deque(maxlen=self.capacity) if self.capacity else deque()
        self._stamps: Deque[datetime] = deque(maxlen=self.capacity) if self.capacity else deque()

    @property
    def enabled(self) -> bool:
        return self.capacity > 0

    def push(self, frame: np.ndarray, when: datetime) -> None:
        if not self.enabled:
            return
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if not ok:
            return
        self._items.append(buf.tobytes())
        self._stamps.append(when)

    def drain(self) -> list:
        """取出全部缓冲并清空，按时间从旧到新。"""
        out = list(self._items)
        self._items.clear()
        self._stamps.clear()
        return out

    def clear(self) -> None:
        self._items.clear()
        self._stamps.clear()

    def __len__(self) -> int:
        return len(self._items)

    def bytes_held(self) -> int:
        return sum(len(b) for b in self._items)


class Recorder:
    """把一段录像写进 mp4。所有编解码只发生在后台线程。

    用法（watcher 主循环里）：
        rec.observe(frame, now, has_person)  # 每帧
        rec.start(path, now)                 # 确认有人进入时
        rec.stop(reason)                     # 策略说该停时
    """

    def __init__(self, cfg: dict, fps: float):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.fps = max(1.0, float(fps))
        self.codec_preference = [
            c.strip() for c in str(cfg.get("codec", "avc1")).split(",") if c.strip()
        ]
        self.queue_size = max(1, int(cfg.get("queue_size", 120)))
        self.join_timeout = float(cfg.get("join_timeout_seconds", 8.0))
        self.draw_boxes = bool(cfg.get("draw_boxes", False))
        self.jpeg_quality_pre = int(cfg.get("pre_roll_jpeg_quality", 80))

        pre_seconds = float(cfg.get("pre_roll_seconds", 8.0))
        self.pre_roll = PreRollBuffer(pre_seconds, self.fps, self.jpeg_quality_pre)

        self._queue: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._writer = None
        self._size: Optional[tuple] = None
        self._path: Optional[str] = None
        self._started_at: Optional[datetime] = None

        self.written = 0
        self.dropped = 0
        self.last_error: Optional[str] = None
        self.segments = 0
        # 本段实际倒进去的预录帧数。音频合并要靠它算「该补多少毫秒静音」——
        # 必须用实际值，因为刚启动时环形缓冲还没攒满，会短于配置值。
        self.last_pre_frames = 0

    # ---------- 查询 ----------

    @property
    def recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_path(self) -> Optional[str]:
        return self._path if self.recording else None

    # ---------- 主循环侧（必须足够快）----------

    def observe(self, frame: np.ndarray, when: datetime, has_person: bool = True) -> None:
        """每帧调用。录像中就投喂写队列，否则把帧攒进预录缓冲。"""
        if not self.enabled:
            return
        if frame is None:
            return

        if self.recording:
            self._enqueue(frame)
        else:
            self.pre_roll.push(frame, when)

    def _enqueue(self, frame: np.ndarray) -> None:
        """丢进队列。队列满就丢这一帧——宁可掉帧，也不能让主循环卡住。"""
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 50 == 0:
                log.warning(
                    "录像队列已满，丢帧 %d 帧（编码跟不上 %s fps，"
                    "可调 record.queue_size 或降低 camera.fps）",
                    self.dropped,
                    self.fps,
                )

    # ---------- 生命周期 ----------

    def start(self, path: str, when: datetime) -> Optional[str]:
        """开录。成功返回真实写入路径（扩展名可能随兜底编码器变）。"""
        if not self.enabled or self.recording:
            return None

        size = self._decide_size()
        if size is None:
            self.last_error = "尚未确定画面尺寸"
            return None

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        writer, used_codec, real_path = self._try_open(path, size)
        if writer is None:
            self.last_error = "所有候选编码器都打不开"
            log.error("录像启动失败：%s（试过 %s）", self.last_error, self.codec_preference)
            return None

        self._writer = writer
        self._size = size
        self._path = real_path
        self._started_at = when
        self._queue = queue.Queue(maxsize=self.queue_size)
        self.written = 0
        self.dropped = 0

        # 先把预录的画面倒进去，顺序在前 = 视频开头就是进门那一刻
        pre = self.pre_roll.drain()
        self.last_pre_frames = len(pre)
        for buf in pre:
            frame = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            self._queue.put_nowait(self._fit(frame))

        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="video-writer")
        self._thread.start()
        self.segments += 1

        log.info("开始录像: %s（%s，预录 %d 帧）", real_path, used_codec, len(pre))
        return real_path

    def stop(self, reason: str = "") -> Optional[str]:
        """收尾。排空队列→释放 writer，全程在写线程完成。"""
        if not self.recording:
            return self._path

        path = self._path
        self._queue.put(None)  # 哨兵，通知线程收尾
        self._thread.join(timeout=self.join_timeout)

        if self._thread.is_alive():
            self.last_error = "写线程未在超时内退出，文件可能不完整"
            log.error("%s: %s", self.last_error, path)
        else:
            secs = (datetime.now() - self._started_at).total_seconds() if self._started_at else 0
            extra = f"，原因：{reason}" if reason else ""
            log.info(
                "录像结束: %s（%.0f 秒 / %d 帧%s%s）",
                path,
                secs,
                self.written,
                f"，丢帧 {self.dropped}" if self.dropped else "",
                extra,
            )

        self._thread = None
        self._queue = None
        self._writer = None
        self._started_at = None
        self._path = None
        return path

    def shutdown(self) -> None:
        """进程退出前调用：确保最后一段录像被正确收尾。"""
        if self.recording:
            self.stop("监控退出")
        self.pre_roll.clear()

    # ---------- 内部 ----------

    def _decide_size(self) -> Optional[tuple]:
        if self._size:
            return self._size
        return None

    def prime_size(self, size: tuple) -> None:
        """把当前画面尺寸告诉录像器——摄像头给多大就录多大。"""
        self._size = size

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        """统一尺寸。摄像头中途被重开时分辨率可能变，VideoWriter 不接受中途换尺寸。"""
        if self._size is None:
            self._size = (frame.shape[1], frame.shape[0])
            return frame
        w, h = self._size
        if frame.shape[1] != w or frame.shape[0] != h:
            return cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        return frame

    def _try_open(self, path: str, size: tuple):
        """按偏好依次试编码器。返回 (writer, codec说明, 真实路径)。"""
        root, ext = os.path.splitext(path)
        # 用户偏好里可能写了没测过的编码器，先试它，再按内置顺序兜底
        tried = []
        for name in self.codec_preference + [c for c, _ in CODEC_CHAIN]:
            if name in tried:
                continue
            tried.append(name)
            target = path if ext.lower() in (".mp4", ".avi") else root + ".mp4"
            w = _open_writer(target, name, self.fps, size)
            if w is not None:
                desc = dict(CODEC_CHAIN).get(name, name)
                return w, desc, target
        return None, None, None

    def _write_loop(self) -> None:
        """后台线程：唯一的 writer 使用者。"""
        try:
            while True:
                frame = self._queue.get()
                if frame is None:
                    break
                try:
                    self._writer.write(frame)
                    self.written += 1
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"写帧失败: {e}"
                    log.error("录像写帧失败: %s", e)
                    break
        finally:
            try:
                if self._writer is not None:
                    self._writer.release()
            except Exception as e:  # noqa: BLE001
                log.error("释放录像 writer 失败: %s", e)


def video_path_for(snapshot_path: str) -> str:
    """抓拍 120530.jpg -> 录像 120530.mp4，两个文件同名并列，事后配对很直观。"""
    root, _ext = os.path.splitext(snapshot_path)
    return root + ".mp4"
