"""房间进入检测主程序。

用法：
    python watcher.py                 # 正式运行
    python watcher.py --dry-run       # 只抓拍存档，不推送（先跑这个验证画面）
    python watcher.py --probe         # 打开摄像头拍一张 preview.jpg 然后退出
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import platform
import shutil
import sqlite3
import sys
import time
from datetime import datetime

import cv2
import yaml

import json

from logic import (
    ACTION_BEST,
    ACTION_FIRE,
    ACTION_START,
    RECORD_STOP_MAX,
    Detection,
    RecordPolicy,
    TriggerState,
    gap_seconds,
    humanize_duration,
    is_stale,
    iter_media_files,
    prune_by_size,
    prune_old,
    should_heartbeat,
    should_speak,
    snapshot_path,
    usage_bytes,
)
import audio_device
import loudness
import speaker
from audio_recorder import AudioRecorder, pre_roll_delay_ms
from faces import FaceFinder
from identity import LABEL_OWNER, LABEL_UNKNOWN, Identity, OwnerTracker
from notifier import Notifier
from recorder import Recorder, video_path_for
from speaker import Speaker

log = logging.getLogger("room-watch")

BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def idle_seconds_windows() -> float:
    """Windows 上取「距上次键鼠输入」的秒数。非 Windows 一律返回 0。"""
    if platform.system() != "Windows":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return millis / 1000.0


class EventStore:
    """极简 SQLite 事件日志，方便事后按时间查。"""

    # 录像功能上线时给表加了一列。老库里没有这列，
    # 而 CREATE TABLE IF NOT EXISTS 对「表已存在」是不做任何事的——
    # 所以新列必须由这里显式补齐，否则写事件直接报错。
    SCHEMA_COLUMNS = {"video": "TEXT"}

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   snapshot TEXT NOT NULL,
                   confidence REAL,
                   notified INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self.conn.commit()
        added = self._migrate()
        if added:
            log.info("事件表已补上列: %s", "、".join(added))

    def _migrate(self) -> list:
        """把新版本才有的列补进旧表。返回新增的列名。"""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(events)")}
        added = []
        for col, decl in self.SCHEMA_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
                added.append(col)
        if added:
            self.conn.commit()
        return added

    def add(
        self,
        ts: datetime,
        snapshot: str,
        conf: float,
        notified: bool,
        video: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, snapshot, confidence, notified, video) "
            "VALUES (?,?,?,?,?)",
            (ts.isoformat(), snapshot, conf, int(notified), video),
        )
        self.conn.commit()


class Watcher:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run

        cam = cfg.get("camera") or {}
        self.cam_index = int(cam.get("index", 0))
        self.backend = BACKENDS.get(str(cam.get("backend", "dshow")).lower(), cv2.CAP_DSHOW)
        self.width = int(cam.get("width", 1280))
        self.height = int(cam.get("height", 720))
        self.fps = int(cam.get("fps", 10))

        det = cfg.get("detector") or {}
        self.model_name = det.get("model", "yolov8n.pt")
        self.det_conf = float(det.get("conf", 0.5))
        self.imgsz = int(det.get("imgsz", 640))
        self.state = TriggerState(
            trigger_frames=int(det.get("trigger_frames", 3)),
            capture_window=int(det.get("capture_window", 8)),
            cooldown_seconds=float(det.get("cooldown_seconds", 90)),
            min_conf=self.det_conf,
        )

        snap = cfg.get("snapshot") or {}
        self.snap_dir = snap.get("dir", "captures")
        self.keep_days = int(snap.get("keep_days", 30))
        self.jpeg_quality = int(snap.get("jpeg_quality", 90))
        self.draw_boxes = bool(snap.get("draw_boxes", True))

        behavior = cfg.get("behavior") or {}
        self.skip_when_active = bool(behavior.get("skip_when_user_active", True))
        self.idle_threshold = float(behavior.get("idle_threshold_seconds", 120))

        voice = cfg.get("voice") or {}
        self.voice_cooldown = float(voice.get("cooldown_seconds", 60))
        self.require_face = bool(voice.get("require_face", True))
        self.speaker = Speaker(voice)
        # 代码里把音频做到满刻度只解决一半问题：如果 Windows 主音量只有 10%，
        # 下游照样砍掉 20 dB。所以运行时把主音量保底，退出时还原。
        self.volume_guard = audio_device.VolumeGuard(
            float(voice.get("ensure_system_volume", 70)),
            bool(voice.get("restore_system_volume", True)),
        )

        face_cfg = cfg.get("face") or {}
        self.face_every_n = max(1, int(face_cfg.get("every_n_frames", 3)))
        self.face_finder = FaceFinder(face_cfg)

        # 人脸识别：区分「你」和「别人」。没注册模板时 identity.usable 为假，
        # 此时一律按访客处理（安全优先，绝不静默漏报）。
        idn_cfg = cfg.get("identity") or {}
        self.identity = Identity(idn_cfg)
        self.owner_tracker = OwnerTracker(
            confirm_frames=int(idn_cfg.get("confirm_frames", 2))
        )
        self.unknown_policy = str(idn_cfg.get("unknown_policy", "alert")).strip().lower()
        self.on_owner = str(idn_cfg.get("on_owner", "skip")).strip().lower()
        # 默认按「他人」开始：还没识别过的时候，行为要和没装识别功能一样
        self.last_identity_label = "other"
        self.last_identity_score = 0.0
        self.identity_hits = 0

        heartbeat = cfg.get("heartbeat") or {}
        self.heartbeat_minutes = float(heartbeat.get("interval_minutes", 30))
        self.stale_seconds = float(heartbeat.get("stale_after_seconds", 120))

        storage = cfg.get("storage") or {}
        self.status_path = storage.get("status", "status.json")
        self.store = EventStore(storage.get("database", "events.db"))
        self.notifier = Notifier(cfg.get("notify") or {})

        # 容量上限：keep_days 管不住「一天之内写满盘」，所以再加一道体积闸门
        self.max_total_bytes = int(float(storage.get("max_total_gb", 20)) * (1024 ** 3))
        self.min_free_bytes = int(float(storage.get("min_free_gb", 3)) * (1024 ** 3))
        self.storage_check_seconds = float(storage.get("check_minutes", 10)) * 60.0
        self.storage_protect_seconds = float(storage.get("protect_seconds", 300))
        self.last_storage_check = None

        rec_cfg = cfg.get("record") or {}
        self.recorder = Recorder(rec_cfg, self.fps)
        # 音轨单独一路：VideoWriter 写不了声音，录成 WAV 收尾再并进 mp4
        self.audio = AudioRecorder(rec_cfg.get("audio") or {})
        # 「录到什么时候为止」是纯逻辑，放在 logic 里便于单测
        self.record_policy = RecordPolicy(
            min_seconds=float(rec_cfg.get("min_seconds", 20.0)),
            idle_stop_seconds=float(rec_cfg.get("idle_stop_seconds", 15.0)),
            max_seconds=float(rec_cfg.get("max_seconds", 300.0)),
            max_total_seconds=float(rec_cfg.get("max_total_seconds", 1800.0)),
        )
        self.current_video: str | None = None

        self.cap = None
        self.model = None
        self.candidate_frame = None
        self.candidate_dets = []

        self.started_at = None
        self.last_frame_at = None
        self.last_heartbeat_at = None
        self.frames = 0
        self.last_spoke_at = None
        self.spoke_this_event = False
        self.empty_streak = 0
        # 连续这么久没检测到人，才算「这一波人走了」，语音重新武装。
        # 否则人一直站在原地，每 90 秒就会被自己的监控播报一次。
        self.empty_reset_frames = max(5, int(self.fps * 3))
        self.events_today = 0
        self.today = datetime.now().date()
        self.fail_streak = 0

    def open_camera(self) -> bool:
        self.cap = cv2.VideoCapture(self.cam_index, self.backend)
        if not self.cap.isOpened():
            log.error("打不开摄像头 index=%s，试试改 config.yaml 里的 camera.index", self.cam_index)
            return False
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        log.info("摄像头已打开：%sx%s (请求 %sx%s)", actual_w, actual_h, self.width, self.height)
        return True

    def load_model(self) -> None:
        from ultralytics import YOLO

        log.info("加载模型 %s（首次运行会自动下载，几 MB）", self.model_name)
        self.model = YOLO(self.model_name)

    def detect(self, frame) -> list:
        results = self.model.predict(
            frame, classes=[0], conf=self.det_conf, imgsz=self.imgsz, verbose=False
        )
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return []
        out = []
        for b in boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            out.append(Detection(x1=x1, y1=y1, x2=x2, y2=y2, conf=float(b.conf[0])))
        return out

    def user_is_active(self) -> bool:
        """人在电脑前（刚有键鼠输入）就不推送，避免给自己发自己的照片。"""
        if not self.skip_when_active:
            return False
        return idle_seconds_windows() < self.idle_threshold

    # ---------- 身份识别 ----------

    def identify(self, frame, dets: list) -> str:
        """判断画面里的人是谁，返回 owner / other / unknown。

        只在「事件关键帧」上调用（ACTION_START / BEST），不是每帧都算——
        识别本身只要几毫秒，但没必要在没人的画面上浪费。

        没注册模板或模型缺失时返回 other：宁可把你自己当访客多报几次，
        也不能因为识别不可用就把所有人都静默放过去。
        """
        self.last_identity_score = 0.0
        if not dets:
            return LABEL_UNKNOWN

        if not self.identity.usable:
            return "other"

        hits = self.face_finder.find_full(frame, dets)
        if not hits:
            # 背影、侧脸、太远：看不到脸就没法认
            return LABEL_UNKNOWN

        labels = []
        for hit in hits:
            if hit.row is None:
                continue
            lab, score = self.identity.identify(frame, hit.row)
            labels.append((lab, score))
        if not labels:
            return LABEL_UNKNOWN

        label, score = self.identity.decide(labels)
        self.last_identity_score = score
        self.identity_hits = len(labels)
        return label

    def is_owner_event(self) -> bool:
        """这次事件该不该按「本人」静默处理。

        三条前提，缺一不可：
        1. 识别功能真的可用（模型在 + 已注册），否则一律不静默
        2. 多帧投票已经确认是本人
        3. unknown_policy=ignore 时，看不清脸也算本人（按配置放行）
        """
        if not self.identity.usable:
            return False
        if self.owner_tracker.owner_present:
            return True
        if self.unknown_policy == "ignore" and self.last_identity_label == LABEL_UNKNOWN:
            return True
        return False

    def annotate(self, frame, dets: list, when: datetime):
        img = frame.copy()
        for d in dets:
            cv2.rectangle(
                img, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), (0, 0, 255), 2
            )
        label = when.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(
            img, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA
        )
        return img

    def save_snapshot(self, frame, dets: list, when: datetime) -> str:
        path = snapshot_path(self.snap_dir, when)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img = self.annotate(frame, dets, when) if self.draw_boxes else frame
        ok = cv2.imwrite(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if not ok:
            raise IOError(f"写图失败: {path}")
        return path

    def write_status(self, camera_ok: bool = True) -> None:
        """把「我还在跑」写成一个文件，供 gallery 的 /status 读取。

        这是回答「监控还在线吗」最直接的依据：看 last_frame_at 距现在多久。
        """
        now = datetime.now()
        payload = {
            "pid": os.getpid(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime": humanize_duration(
                (now - self.started_at).total_seconds() if self.started_at else 0
            ),
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "seconds_since_frame": (
                round((now - self.last_frame_at).total_seconds(), 1)
                if self.last_frame_at
                else None
            ),
            "stale": is_stale(self.last_frame_at, now, self.stale_seconds),
            "camera_ok": camera_ok,
            "frames": self.frames,
            "events_today": self.events_today,
            "recording": self.recorder.recording,
            "dry_run": self.dry_run,
        }
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.status_path)

    def maybe_speak(self, frame, dets: list, now: datetime) -> None:
        """有人进入时，对现场的人播报语音提示。

        只在「确认有人」之后调用。require_face 为真时会先找脸，
        找到才开口——对着背影说话没有意义，也容易误伤路过的人。
        """
        if not self.speaker.enabled or self.spoke_this_event:
            return

        face_found = False
        if self.require_face:
            # 人脸检测不必每帧都跑，隔几帧一次，省 CPU
            if self.frames % self.face_every_n != 0:
                return
            faces = self.face_finder.find(frame, dets)
            face_found = len(faces) > 0

        if not should_speak(
            now, self.last_spoke_at, self.voice_cooldown, face_found, self.require_face
        ):
            return

        self.speaker.say()
        self.last_spoke_at = now
        self.spoke_this_event = True
        log.info("语音提示已触发（检测到人脸: %s）", "是" if face_found else "否")

    # ---------- 录像 ----------

    def decorate_video_frame(self, frame, dets: list, when: datetime):
        """给录像帧叠时间戳。帧来自 cap.read()，要改必须先 copy。

        人形框默认不画：事后回看时，跳来跳去的框会挡住脸和手部动作，
        而取证要看的恰恰是这些细节。
        """
        img = frame.copy()
        if getattr(self.recorder, "draw_boxes", False) and dets:
            for d in dets:
                cv2.rectangle(
                    img, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), (0, 0, 255), 2
                )
        label = when.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 0, 255), 2, cv2.LINE_AA)
        return img

    def start_recording(self, dets: list, when: datetime, continuing: bool = False) -> None:
        """确认有人进入的那一帧就开录，不等抓拍完成。

        为什么要赶在 ACTION_START 而不是 ACTION_FIRE：FIRE 要等采集窗口或人离开，
        那时最想看的「推门、进门、往哪走」早过去了。
        """
        if not self.recorder.enabled or self.recorder.recording:
            return
        target = video_path_for(snapshot_path(self.snap_dir, when))
        started = self.recorder.start(target, when)
        if started:
            self.current_video = started
            self.record_policy.begin(when, has_person=bool(dets), continuing=continuing)
            # 音轨与视频同步开录。这里不记预录帧数——stop 时读 recorder.last_pre_frames，
            # 因为那时才知道这一段的预录到底有多少帧（可能不足配置值）。
            if self.audio.enabled and self.audio.available:
                self.audio.start(os.path.splitext(started)[0] + ".wav")
            if continuing:
                log.info("上一段到点，已续录到下一段: %s", started)
        else:
            log.warning("录像未能启动（%s）", self.recorder.last_error)

    def stop_recording(self, reason: str, now: datetime, discard: bool = False) -> None:
        """收尾一段录像。

        discard=True 表示这段作废（识别为本人）：连音频一起删掉，
        不做 ffmpeg 合并——没必要为一段要删的东西先花几百毫秒合并。
        """
        if not self.recorder.recording:
            return
        # 先结算时长额度，再真正收文件
        self.record_policy.finish(now)
        pre_frames = self.recorder.last_pre_frames  # stop() 之后该值会被重置，先取
        video = self.recorder.stop(reason)
        wav = self.audio.stop() if self.audio.recording else None
        self.current_video = None

        if discard:
            for path in (video, wav):
                if not path:
                    continue
                try:
                    os.remove(path)
                except OSError:
                    pass
            if video:
                log.info("识别为本人，本段录像已丢弃: %s", video)
            return

        if video and wav:
            # 视频开头是预录画面（当时还没开始录音），音轨要补等长静音才对得上
            self.audio.merge_async(video, wav, pre_roll_delay_ms(pre_frames, self.fps))

    def pump_recording(self, frame, dets: list, now: datetime) -> None:
        """每帧一次：喂帧 + 判断该不该收尾。"""
        if not self.recorder.enabled:
            return

        # 先更新「最后一次看见人」，再判定停止，避免把最后一帧当成离开
        self.record_policy.note(now, bool(dets))

        if self.recorder.recording:
            self.recorder.observe(self.decorate_video_frame(frame, dets, now), now, bool(dets))
        else:
            # 没在录的时候，帧进预录缓冲，攒着给下一段录像当开头。
            # 时间戳在这里就要盖上——不然成片开头（pre-roll 部分）没有时间信息，
            # 而那几秒恰恰是「人还没进门」的关键时刻。
            self.recorder.observe(self.decorate_video_frame(frame, [], now), now, bool(dets))

        reason = self.record_policy.check(now)
        if not reason:
            return

        # 单段到点了但人还在：接一段新的，别把后半场丢下。
        # 额度要在收尾「之前」看——收尾会把这段时长加进累计里。
        carry_on = reason == RECORD_STOP_MAX and bool(dets) and not self.record_policy.exhausted
        self.stop_recording(reason, now)
        if carry_on:
            self.start_recording(dets, now, continuing=True)

    def enforce_storage(self, now: datetime) -> None:
        """容量闸门：超过上限就删最旧的，磁盘快满时再逼近清理一次。

        keep_days 按天清理管不住「今天来了一整天」这种极端情况——
        一天之内照样能写出几十 GB。所以这里按体积再管一道。
        """
        if self.max_total_bytes <= 0 and self.min_free_bytes <= 0:
            return
        if self.last_storage_check is not None:
            elapsed = (now - self.last_storage_check).total_seconds()
            if elapsed < self.storage_check_seconds:
                return
        self.last_storage_check = now

        removed = prune_by_size(
            self.snap_dir, self.max_total_bytes, now, self.storage_protect_seconds
        )
        if removed:
            used = usage_bytes(self.snap_dir) / (1024 ** 3)
            log.info(
                "容量清理：删掉 %d 个最旧文件，当前占用 %.2f GB（上限 %.2f GB）",
                len(removed),
                used,
                self.max_total_bytes / (1024 ** 3),
            )

        if self.min_free_bytes <= 0:
            return
        try:
            free = shutil.disk_usage(self.snap_dir).free
        except OSError:
            return
        if free >= self.min_free_bytes:
            return

        log.warning(
            "磁盘剩余仅 %.2f GB（低于 %.2f GB），逼近清理到上限的一半",
            free / (1024 ** 3),
            self.min_free_bytes / (1024 ** 3),
        )
        more = prune_by_size(
            self.snap_dir,
            max(1, self.max_total_bytes // 2),
            now,
            self.storage_protect_seconds,
        )
        if more:
            log.info("额外清理 %d 个文件", len(more))

    def maybe_heartbeat(self, now: datetime) -> None:
        """定时报平安。收不到心跳，就说明它挂了。"""
        if not should_heartbeat(now, self.last_heartbeat_at, self.heartbeat_minutes):
            return
        self.last_heartbeat_at = now

        if self.dry_run:
            log.info("[心跳-仅日志] 监控在线 %s，今日抓拍 %d 次",
                     humanize_duration((now - self.started_at).total_seconds()),
                     self.events_today)
            return

        text = (
            f"监控在线 · 已运行 {humanize_duration((now - self.started_at).total_seconds())}\n"
            f"今日抓拍 {self.events_today} 次 · 最后画面 "
            f"{self.last_frame_at.strftime('%H:%M:%S') if self.last_frame_at else '未知'}"
        )
        self.notifier.send("监控在线", text)

    def fire_event(self, frame, dets: list, when: datetime) -> None:
        conf = max((d.conf for d in dets), default=0.0)
        path = self.save_snapshot(frame, dets, when)
        self.events_today += 1
        log.info("事件已抓拍: %s (置信度 %.2f)，今日第 %d 次", path, conf, self.events_today)
        if self.current_video:
            log.info("配套录像: %s", self.current_video)

        notified = False
        if self.dry_run:
            log.info("dry-run 模式，跳过推送")
        elif self.user_is_active():
            log.info("检测到你本人正在电脑前，跳过推送")
        else:
            text = f"{when.strftime('%H:%M:%S')} 检测到有人进入房间\n存档: {path}"
            if self.current_video:
                text += f"\n录像: {self.current_video}"
            with open(path, "rb") as f:
                notified = self.notifier.send(text, image_bytes=f.read())

        self.store.add(when, path, conf, notified, video=self.current_video)

    def run(self) -> None:
        if not self.open_camera():
            sys.exit(2)
        self.load_model()

        os.makedirs(self.snap_dir, exist_ok=True)
        prune_old(self.snap_dir, self.keep_days, datetime.now())
        log.info("开始监控。Ctrl+C 退出。")

        self.started_at = datetime.now()
        interval = 1.0 / max(1, self.fps)
        last_status_write = 0.0

        log.info("系统音量：%s", self.volume_guard.apply())
        if self.volume_guard.changed:
            log.info("提示：这段音量改动会在监控退出时还原（voice.restore_system_volume）")

        try:
            while True:
                ok, frame = self.cap.read()
                now = datetime.now()

                if not ok:
                    # 摄像头被占用、驱动抽风、USB 掉线都会走到这里
                    self.fail_streak += 1
                    log.warning("读帧失败（连续 %d 次）", self.fail_streak)
                    self.write_status(camera_ok=False)
                    if self.fail_streak >= 10:
                        log.error("连续失败 10 次，尝试重新打开摄像头")
                        self.cap.release()
                        time.sleep(2.0)
                        if self.open_camera():
                            self.fail_streak = 0
                        else:
                            self.fail_streak = 0  # 重置计数，下一轮再试
                    time.sleep(1.0)
                    continue

                self.fail_streak = 0
                self.frames += 1
                self.last_frame_at = now

                if now.date() != self.today:
                    self.today = now.date()
                    self.events_today = 0
                    prune_old(self.snap_dir, self.keep_days, now)

                dets = self.detect(frame)
                h, w = frame.shape[:2]
                action = self.state.observe(dets, now, (w, h))
                # 让录像器知道画面多大——摄像头给多大就录多大
                self.recorder.prime_size((w, h))

                # 画面里连续几秒没有人 = 上一波人走了，语音重新武装
                if dets:
                    self.empty_streak = 0
                else:
                    self.empty_streak += 1
                    if self.empty_streak >= self.empty_reset_frames:
                        self.spoke_this_event = False
                        self.empty_streak = 0
                        # 人都走了：解除「本人已确认」。不重置的话，
                        # 你自己进来一次之后，接下来所有人都会被当成你——这是最危险的失效。
                        if self.owner_tracker.owner_present:
                            log.info("画面已空，解除本人识别状态")
                        self.owner_tracker.reset()
                        self.last_identity_label = "other"

                # 身份识别只在事件关键帧上做：省算力，也避免在空画面上瞎判
                if action in (ACTION_START, ACTION_BEST):
                    self.last_identity_label = self.identify(frame, dets)
                    self.owner_tracker.observe(self.last_identity_label)
                    if self.last_identity_label == LABEL_OWNER:
                        log.info(
                            "识别为本人（相似度 %.2f），本段按静默处理",
                            self.last_identity_score,
                        )

                if action in (ACTION_START, ACTION_BEST):
                    self.candidate_frame = frame.copy()
                    self.candidate_dets = dets
                    # 本人不播报——否则你坐着工作，每过一阵就被自己念一次。
                    # 这里比抓拍更严：**单帧**判定本人就不出声，不等投票确认。
                    # 理由：播报是说错一句就打扰你一整天，而抓拍/录像才是证据，
                    # 那两者仍走 confirm_frames 的确认流程，不能被单帧结果放行。
                    if not self.is_owner_event() and self.last_identity_label != LABEL_OWNER:
                        self.maybe_speak(frame, dets, now)
                elif action == ACTION_FIRE and self.candidate_frame is not None:
                    # 收尾这一帧再认一次：此刻人已站定，脸通常比第一帧清楚。
                    # 少了这一步，若中间帧的「适不适合存档」分数一直没创出新高，
                    # ACTION_BEST 就不会触发、识别投票只有 1 次、达不到 confirm_frames，
                    # 于是本人还是会被抓拍一张（今晚实测就漏了一张 234806.jpg）。
                    self.last_identity_label = self.identify(
                        self.candidate_frame, self.candidate_dets or dets
                    )
                    self.owner_tracker.observe(self.last_identity_label)
                    if self.is_owner_event():
                        log.info(
                            "事件判定为本人（%s，相似度 %.2f），不抓拍、不推送",
                            self.last_identity_label,
                            self.last_identity_score,
                        )
                        self.stop_recording(
                            "识别为本人", now, discard=(self.on_owner == "skip")
                        )
                        self.candidate_frame = None
                        self.candidate_dets = []
                    else:
                        self.fire_event(self.candidate_frame, self.candidate_dets or dets, now)
                        self.candidate_frame = None
                        self.candidate_dets = []

                # 确认进入就开录，不等抓拍流程走完（那时人可能已经走过镜头了）。
                # 但第一帧就认出是本人的话先不开录——等下一帧确认，
                # 免得先录一段自己再删。预录缓冲会把这段画面补回来的。
                if action == ACTION_START:
                    if self.last_identity_label != LABEL_OWNER:
                        self.start_recording(dets, now)
                elif action == ACTION_BEST and not self.recorder.recording:
                    if not self.is_owner_event():
                        self.start_recording(dets, now)

                self.pump_recording(frame, dets, now)
                self.enforce_storage(now)
                self.maybe_heartbeat(now)

                # 状态文件没必要每帧都写，5 秒一次足够回答「还活着吗」
                if time.time() - last_status_write >= 5.0:
                    self.write_status(camera_ok=True)
                    last_status_write = time.time()

                time.sleep(interval)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，退出")
        finally:
            # 录像必须先于状态落盘收尾：这时文件才完整可读。
            # 走 stop_recording 而不是直接 recorder.shutdown()，是为了让音轨也收尾并合并。
            if self.recorder.recording:
                try:
                    self.stop_recording("监控退出", datetime.now())
                except Exception as exc:  # noqa: BLE001
                    log.warning("退出时收尾录像失败: %s", exc)
            else:
                self.audio.stop()
            self.recorder.shutdown()
            self.write_status(camera_ok=False)
            if self.cap:
                self.cap.release()
            self.speaker.shutdown()
            restored = self.volume_guard.restore()
            if restored:
                log.info("系统音量：%s", restored)

    def probe(self) -> None:
        """打开摄像头拍一张 preview.jpg，用来确认画面角度对不对。"""
        if not self.open_camera():
            sys.exit(2)
        ok, frame = self.cap.read()
        if not ok:
            log.error("读不到画面")
            sys.exit(3)
        cv2.imwrite("preview.jpg", frame)
        log.info("已保存 preview.jpg（%sx%s），打开看看角度对不对", frame.shape[1], frame.shape[0])
        self.cap.release()


def _full_frame_dets(frame) -> list:
    """把整幅画面当成一个「人」交给找脸器。

    平时运行时人脸只在 YOLO 框出的人形里找（快且误报少），
    但注册和自测时我们想找的是「这张图里所有的脸」，所以放开到全图。
    """
    h, w = frame.shape[:2]
    return [Detection(0.0, 0.0, float(w), float(h), 1.0)]


def _faces_in_image(frame, ff) -> list:
    return ff.find_full(frame, _full_frame_dets(frame))


def enroll_owner(cfg: dict, image_path: str | None = None, samples: int = 12) -> int:
    """注册本人人脸。返回退出码。"""
    idn = Identity(cfg.get("identity") or {})
    if not idn.embedder.enabled:
        print("人脸识别模型不可用：", idn.embedder.last_error)
        print("请确认 models/face_recognition_sface_2021dec.onnx 已完整下载（约 37MB）")
        return 2

    ff = FaceFinder(cfg.get("face") or {})
    feats = []

    if image_path:
        img = cv2.imread(image_path)
        if img is None:
            print(f"读不到图片：{image_path}")
            return 2
        hits = _faces_in_image(img, ff)
        print(f"{image_path}：找到 {len(hits)} 张脸")
        for hit in hits:
            if hit.row is None:
                continue
            feat = idn.embedder.embed(img, hit.row)
            if feat is not None:
                feats.append(feat)
        if not feats:
            print("这张图里没提取到可用的人脸特征（可能太小/太糊/侧脸）")
            return 3
    else:
        cam = cfg.get("camera") or {}
        backend = BACKENDS.get(str(cam.get("backend", "dshow")).lower(), cv2.CAP_DSHOW)
        cap = cv2.VideoCapture(int(cam.get("index", 0)), backend)
        if not cap.isOpened():
            print("打不开摄像头，注册失败")
            return 2
        print(f"请正对镜头看一会儿，正在采集 {samples} 张（约 {int(samples * 0.5)} 秒）...")
        try:
            tries = 0
            while len(feats) < samples and tries < samples * 12:
                tries += 1
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.1)
                    continue
                hits = _faces_in_image(frame, ff)
                # 只接受「画面里恰好一张脸」：多张可能拍到别人，注册进模板就坏事了
                if len(hits) == 1 and hits[0].row is not None:
                    feat = idn.embedder.embed(frame, hits[0].row)
                    if feat is not None:
                        feats.append(feat)
                        print(f"  已采集 {len(feats)}/{samples}")
                time.sleep(0.5)
        finally:
            cap.release()

    # 从一张图注册时 1 个样本就是正常的；摄像头采集才要求多个（多角度更稳）
    min_samples = 1 if image_path else 3
    if len(feats) < min_samples:
        print(f"有效样本太少（{len(feats)} 个），至少需要 {min_samples} 个。")
        if not image_path:
            print("换个光线好、正对镜头的位置重试；或者用 --enroll-from 指定一张清晰的抓拍。")
        return 3

    if not idn.save_owner(feats):
        return 4
    print(f"注册完成：{idn.owner_file}（{len(feats)} 个样本，阈值 {idn.threshold}）")
    print("接下来可以验证一下：python watcher.py --whoami <某张你自己的截图>")
    return 0


def whoami(cfg: dict, image_path: str) -> int:
    """测试识别：这张图里的人是不是你。"""
    idn = Identity(cfg.get("identity") or {})
    if not idn.usable:
        print("识别不可用：模型缺失，或还没注册本人（先跑 python watcher.py --enroll）")
        return 2

    img = cv2.imread(image_path)
    if img is None:
        print(f"读不到图片：{image_path}")
        return 2

    ff = FaceFinder(cfg.get("face") or {})
    hits = _faces_in_image(img, ff)
    if not hits:
        print("这张图里没找到脸")
        return 3

    print(f"{image_path}：找到 {len(hits)} 张脸（阈值 {idn.threshold}）")
    for i, hit in enumerate(hits, 1):
        if hit.row is None:
            print(f"  第 {i} 张：没有关键点，无法识别")
            continue
        label, score = idn.identify(img, hit.row)
        mark = {"owner": "✅ 本人", "other": "⚠ 他人", "unknown": "？认不出"}[label]
        print(f"  第 {i} 张：{mark}  相似度 {score:.3f}")
    return 0


def print_storage(config_path: str) -> None:
    """看存储占用、上限，以及最旧/最新文件。"""
    cfg = load_config(config_path)
    snap = cfg.get("snapshot") or {}
    directory = snap.get("dir", "captures")
    st = cfg.get("storage") or {}
    max_gb = float(st.get("max_total_gb", 20))
    min_free_gb = float(st.get("min_free_gb", 3))
    keep_days = int(snap.get("keep_days", 30))

    if not os.path.isdir(directory):
        print(f"存档目录还不存在：{directory}")
        return

    files = iter_media_files(directory)
    total = sum(size for _m, _p, size in files)

    print("=== 存储占用 ===")
    print(f"目录        : {os.path.abspath(directory)}")
    print(f"媒体文件    : {len(files)} 个")
    print(f"当前占用    : {total / (1024 ** 3):.2f} GB")
    print(f"容量上限    : {max_gb:.0f} GB" + ("（0 = 不限）" if max_gb <= 0 else ""))
    print(f"按天保留    : {keep_days} 天" + ("（0 = 不限）" if keep_days <= 0 else ""))
    try:
        free = shutil.disk_usage(directory).free
        print(f"磁盘剩余    : {free / (1024 ** 3):.2f} GB（低于 {min_free_gb:.0f} GB 会逼近清理）")
    except OSError:
        pass

    if files:
        def fmt(ts: float) -> str:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        oldest = min(files, key=lambda x: x[0])
        newest = max(files, key=lambda x: x[0])
        print(f"最旧文件    : {fmt(oldest[0])}  {oldest[1]}")
        print(f"最新文件    : {fmt(newest[0])}  {newest[1]}")

        if max_gb > 0:
            over = total - max_gb * (1024 ** 3)
            if over > 0:
                print(f"⚠ 已超出上限 {over / (1024 ** 3):.2f} GB，下一轮检查会删掉最旧的文件")

        by_ext: dict = {}
        for _m, path, size in files:
            ext = os.path.splitext(path)[1].lower()
            cnt, sz = by_ext.get(ext, (0, 0))
            by_ext[ext] = (cnt + 1, sz + size)
        print("按类型      :")
        for ext, (cnt, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1]):
            print(f"  {ext:<6s} {cnt:5d} 个   {size / (1024 ** 2):9.1f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description="房间进入检测与抓拍")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="抓拍但不推送")
    parser.add_argument("--probe", action="store_true", help="只拍一张预览图然后退出")
    parser.add_argument("--status", action="store_true", help="打印监控是否还在线")
    parser.add_argument("--say", action="store_true", help="试听语音提示（响度处理后的版本）然后退出")
    parser.add_argument("--say-raw", action="store_true", help="试听未处理的原始语音，用来做对比")
    parser.add_argument("--ab", action="store_true", help="先放原始版再放处理版，直接听差别")
    parser.add_argument("--volume-check", action="store_true", help="检查系统输出音量，看瓶颈在哪")
    parser.add_argument("--volume-set", type=float, metavar="PCT",
                        help="直接设置系统主音量（0~100）然后退出")
    parser.add_argument("--log", default=None, help="把日志同时写进这个文件（如 watcher.log）")
    parser.add_argument("--enroll", action="store_true",
                        help="注册本人人脸：正对镜头采集十几张，建立「本人模板」")
    parser.add_argument("--enroll-from", metavar="IMG", default=None,
                        help="从一张已有图片注册本人人脸（比如某张清楚的抓拍）")
    parser.add_argument("--whoami", metavar="IMG", default=None,
                        help="测试识别：判断这张图里的人是不是你")
    parser.add_argument("--storage", action="store_true",
                        help="查看存储占用、容量上限与最旧文件")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    # comtypes（音频设备读取的底层库）会刷一堆 INFO，压掉
    for noisy in ("comtypes", "comtypes.client", "comtypes.gen"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.volume_check:
        print(audio_device.describe())
        return

    if args.volume_set is not None:
        ok = audio_device.set_master_percent(args.volume_set)
        print(f"设置主音量 -> {args.volume_set:.0f}%: {'成功' if ok else '失败'}")
        print()
        print(audio_device.describe())
        return

    if args.status:
        print_status(args.config)
        return

    if args.storage:
        print_storage(args.config)
        return

    cfg = load_config(args.config)

    if args.enroll or args.enroll_from:
        sys.exit(enroll_owner(cfg, image_path=args.enroll_from))

    if args.whoami:
        sys.exit(whoami(cfg, args.whoami))

    if args.say or args.say_raw or args.ab:
        voice_cfg = cfg.get("voice") or {}
        if not voice_cfg.get("enabled", True):
            print("语音提示在 config.yaml 里被关掉了（voice.enabled: false）")
            return
        if args.say_raw:
            speaker.play_raw(voice_cfg)
        elif args.ab:
            speaker.ab_compare(voice_cfg)
        else:
            sp = Speaker(voice_cfg)
            print(f"试听（处理版）：{sp.text}")
            sp.say()
            time.sleep(4)  # 等后台线程合成、处理、播完
            if sp.last_report:
                print()
                print("\n".join(loudness.report_lines(sp.last_report)))
            print(f"播放来源：{sp.last_source or '未知'}")
            sp.shutdown()
        return

    w = Watcher(cfg, dry_run=args.dry_run)
    if args.probe:
        w.probe()
    else:
        w.run()


def print_status(config_path: str) -> None:
    """读 watcher 写下的 status.json，判断它是否还活着。"""
    cfg = load_config(config_path)
    path = (cfg.get("storage") or {}).get("status", "status.json")

    if not os.path.exists(path):
        print("监控未运行（找不到 status.json）")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        print(f"状态文件损坏或读不了：{path}")
        return

    # 关键：绝不能信文件里存的 stale 字段。
    # 进程被强制杀掉时，它留下的最后一份状态永远是 stale=false，
    # 照着读就会把「早就死了」报成「在线」。必须在读取这一刻重算。
    tolerance = float((cfg.get("heartbeat") or {}).get("stale_after_seconds", 120))
    gap = gap_seconds(s.get("last_frame_at"))
    stale = gap is None or gap > tolerance
    verdict = "已停止 / 无画面" if stale else "在线"

    print(f"状态        : {verdict}")
    print(f"进程 PID    : {s.get('pid')}")
    print(f"启动时间    : {s.get('started_at')}")
    print(f"已运行      : {s.get('uptime')}")
    ago = f"{gap:.0f} 秒前" if gap is not None else "未知"
    print(f"最后画面    : {s.get('last_frame_at')}（{ago}）")
    print(f"摄像头      : {'正常' if s.get('camera_ok') else '异常'}")
    print(f"已处理帧数  : {s.get('frames')}")
    print(f"今日抓拍    : {s.get('events_today')} 次")
    if s.get("dry_run"):
        print("模式        : dry-run（只存档不推送）")


if __name__ == "__main__":
    main()
