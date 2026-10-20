"""房间进入检测主程序。

用法：
    rw start                          # 正式运行
    rw dry-run                        # 只抓拍存档，不推送（先跑这个验证画面）
    rw probe                          # 打开摄像头拍一张预览图然后退出
"""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import logging
import os
import platform
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import yaml

import json

from .rules import (
    ACTION_BEST,
    ACTION_FIRE,
    ACTION_START,
    RECORD_STOP_MAX,
    STORAGE_OVER,
    STORAGE_WARNING,
    Detection,
    RecordPolicy,
    TriggerState,
    allow_notify,
    allow_record,
    gap_seconds,
    humanize_duration,
    is_stale,
    iter_media_files,
    normalize_owner_policy,
    prune_by_size,
    prune_old,
    should_heartbeat,
    should_speak,
    snapshot_path,
    storage_forecast,
    storage_summary,
    usage_bytes,
)
from . import audio_device
from . import control
from . import health
from . import logutil
from . import loudness
from . import paths
from . import selfcheck as selfcheck_mod
from . import speaker
from .diagnostics import Diagnostics
from .paths import resolve
from .config import load_config as _load_config
from . import camera
from .audio_recorder import AudioRecorder, pre_roll_delay_ms
from .detector import FaceFinder
from .gate import MotionGate
from .onnx_detector import build_detector
from .delivery import ChannelDispatcher, DeliveryWorker, build_items, enabled_channels
from .identity import LABEL_OWNER, LABEL_UNKNOWN, Identity, OwnerTracker, save_owner_photo
from .labels import identity_text
from .outbox import OutboxStore
from .recorder import Recorder, video_path_for
from .speaker import Speaker

log = logging.getLogger("room-watch")

# 人进来后最多重试找脸多久。够他走到镜头前转个身，又不会在长期停留时
# 每秒跑一次人脸检测。
VOICE_RETRY_SECONDS = 30.0

# 采集源解析（USB / RTSP / 文件）已搬到 camera.py。
# 主循环只面对 cap 对象，不再关心背后是哪个后端。


# 配置加载（含 config.local.yaml 覆盖与路径归一）已搬到 config.py。
# 这里保留同名入口，避免调用方和历史代码到处改 import。
load_config = _load_config


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

    # 录像功能上线时给表加了一列；0.1.13 又加了身份列。老库里没有这些列，
    # 而 CREATE TABLE IF NOT EXISTS 对「表已存在」是不做任何事的——
    # 所以新列必须由这里显式补齐，否则写事件直接报错。
    SCHEMA_COLUMNS = {"video": "TEXT", "identity": "TEXT"}

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
        identity: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, snapshot, confidence, notified, video, identity) "
            "VALUES (?,?,?,?,?,?)",
            (ts.isoformat(), snapshot, conf, int(notified), video, identity),
        )
        self.conn.commit()


class Watcher:
    def __init__(self, cfg: dict, dry_run: bool = False,
                 diagnostics: Diagnostics | None = None):
        """配置类型写错时按「配置错误」退出，而不是「未处理异常」。

        后者会写 EXIT_UNHANDLED（5），guardian 会当成崩溃并触发恢复/告警；
        而一个 `imgsz: abc` 的笔误应该是可读的配置错误（退出码 2），
        修配置后重启即可，不该进入崩溃统计。
        """
        try:
            self._init_impl(cfg, dry_run, diagnostics)
        except SystemExit:
            raise
        except (ValueError, TypeError) as exc:
            log.error("配置项类型不合法，无法启动：%s（检查 config.yaml 与 config.local.yaml）",
                      exc)
            raise SystemExit(health.EXIT_CONFIG) from exc

    def _init_impl(self, cfg: dict, dry_run: bool = False,
                   diagnostics: Diagnostics | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.diagnostics = diagnostics
        # 停止请求文件放在这个根下的 var/state/，与界面/`rw stop` 共用同一处
        self.root = paths.runtime_root()
        self.probe_json = False

        health_cfg = cfg.get("health") or {}
        self.health_path = resolve(health_cfg.get("path"), "var/state/health.json")
        self.exit_record_path = resolve(
            health_cfg.get("exit_record"), "var/state/last_exit.json"
        )
        self.health_interval = float(health_cfg.get("snapshot_seconds", 1))
        self.stall_seconds = float(health_cfg.get("stall_seconds", 120))

        self.loop_seq = 0
        self.last_inference_at = None
        self.consecutive_errors = 0
        self.camera_reopens = 0
        self.camera_ok = True
        self.selfcheck: list[dict] = []
        self.exit_reason = "normal"
        self._watchdog_stop = threading.Event()

        cam = cfg.get("camera") or {}
        self.cam_index = int(cam.get("index", 0))
        # camera.source 为空时沿用 camera.index，老配置不用改
        try:
            self.camera_spec = camera.parse_source(cam.get("source"), self.cam_index)
        except ValueError as exc:
            log.error("%s", exc)
            sys.exit(2)
        self.backend_name = str(cam.get("backend", "dshow")).lower()
        self.open_timeout_ms = int(cam.get("open_timeout_ms", 5000))
        self.read_timeout_ms = int(cam.get("read_timeout_ms", 5000))
        self.reconnect_base = float(cam.get("reconnect_seconds", 3.0))
        self.reconnect_max = float(cam.get("reconnect_max_seconds", 60.0))
        # 本机设备偶尔失败一帧很正常，连续 10 次才重开；网络流掉线要快得多才救得回来
        self.reconnect_after = 3 if self.camera_spec.kind == camera.URL else 10
        self.reconnect_attempt = 0
        self.width = int(cam.get("width", 1280))
        self.height = int(cam.get("height", 720))
        self.fps = int(cam.get("fps", 10))

        det = cfg.get("detector") or {}
        self.detector_cfg = det
        self.model_name = det.get("model", "yolov8n.pt")
        # 静止门控：空房间不推理。事件期间自动旁路，见 gate.py 的说明。
        self.gate = MotionGate(cfg.get("gate") or {})
        self.last_detection_at = None
        self.detect_errors = 0
        self.det_conf = float(det.get("conf", 0.5))
        self.imgsz = int(det.get("imgsz", 640))
        self.state = TriggerState(
            trigger_frames=int(det.get("trigger_frames", 3)),
            capture_window=int(det.get("capture_window", 8)),
            cooldown_seconds=float(det.get("cooldown_seconds", 90)),
            min_conf=self.det_conf,
        )

        snap = cfg.get("snapshot") or {}
        self.snap_dir = snap.get("dir") or resolve("data/captures")
        self.keep_days = int(snap.get("keep_days", 30))
        self.jpeg_quality = int(snap.get("jpeg_quality", 90))
        self.draw_boxes = bool(snap.get("draw_boxes", True))

        behavior = cfg.get("behavior") or {}
        # 旧键只表达「要不要推送」；新键 record.owner_at_computer 优先。
        # 保留原始值（不填默认），让 normalize_owner_policy 能区分「没配」和「配了 false」。
        self.legacy_skip_when_active = behavior.get("skip_when_user_active")
        self.idle_threshold = float(behavior.get("idle_threshold_seconds", 120))

        voice = cfg.get("voice") or {}
        self.voice_cooldown = float(voice.get("cooldown_seconds", 60))
        self.require_face = bool(voice.get("require_face", True))
        self.speaker = Speaker(voice)
        # 代码里把音频做到满刻度只解决一半问题：如果 Windows 主音量只有 10%，
        # 下游照样砍掉 20 dB。所以运行时把主音量保底，退出时还原。
        # 音量策略：默认 off，完全不碰系统音量（录音走麦克风，与播放音量无关）。
        # master 档才需要 backup，用于「被强杀时下次补还原」。
        self.volume_mode = audio_device.resolve_mode(voice)
        self.volume_backup = paths.STATE_DIR / "volume_backup.json"
        self.volume_guard = audio_device.VolumeGuard(
            ensure_percent=float(voice.get("ensure_system_volume", 0) or 0),
            restore=bool(voice.get("restore_system_volume", True)),
            mode=self.volume_mode,
            backup_path=self.volume_backup if self.volume_mode == "master" else None,
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
        self.status_path = storage.get("status") or resolve("var/state/status.json")
        # 磁盘满/权限抖动时状态文件可能写不出去；主循环不能因此退出。
        # 只在「好→坏」和「坏→好」时各记一条，避免每秒刷屏。
        self._status_write_failed = False
        self._health_write_failed = False
        event_db = Path(storage.get("database") or resolve("var/db/events.db"))
        self.store = EventStore(str(event_db))
        self.notify_cfg = cfg.get("notify") or {}
        outbox_cfg = cfg.get("outbox") or {}
        outbox_db = Path(outbox_cfg.get("database") or (event_db.parent / "outbox.db"))
        if not outbox_db.is_absolute():
            outbox_db = Path(resolve(str(outbox_db)))
        self.outbox_path = str(outbox_db)
        media_dir = Path(outbox_cfg.get("media_dir") or (event_db.parent.parent / "outbox"))
        if not media_dir.is_absolute():
            media_dir = Path(resolve(str(media_dir)))
        self.outbox_media = str(media_dir)
        self.outbox = OutboxStore(self.outbox_path)
        # outbox 连接属于创建它的线程。录像合并回调在 audio-merge 线程里
        # 调 push_video，若直接复用主线程的连接，sqlite3 会拒绝（跨线程使用）。
        # 记下主线程，异步入队时临时开一条本线程的连接。
        self._outbox_thread = threading.get_ident()
        self._outbox_lock = threading.Lock()
        self.dispatcher = ChannelDispatcher(self.notify_cfg, self.outbox_media)
        max_age_days = float(outbox_cfg.get("max_age_days", 7))
        max_total_bytes = int(float(outbox_cfg.get("max_total_mb", 2048)) * 1024 * 1024)
        common = dict(
            media_dir=self.outbox_media,
            max_age_days=max_age_days,
            max_total_bytes=max_total_bytes,
        )
        self.delivery_fast = DeliveryWorker(
            self.outbox_path, self.dispatcher, kinds=("text", "image"), **common)
        # 视频转码/上传可能几十秒，单独一条慢道，不能挡住文字告警。
        self.delivery_video = DeliveryWorker(
            self.outbox_path, self.dispatcher, kinds=("video",),
            poll_seconds=5.0, **common)

        # 容量上限：keep_days 管不住「一天之内写满盘」，所以再加一道体积闸门
        self.max_total_bytes = int(float(storage.get("max_total_gb", 20)) * (1024 ** 3))
        self.min_free_bytes = int(float(storage.get("min_free_gb", 3)) * (1024 ** 3))
        self.storage_check_seconds = float(storage.get("check_minutes", 10)) * 60.0
        self.storage_protect_seconds = float(storage.get("protect_seconds", 300))
        self.storage_warn_days = float(storage.get("warn_days", 7))
        self.last_storage_check = None
        # 最近一次容量预测。None 表示还没算过（或容量完全不受限）。
        self.storage_forecast: dict | None = None
        # 预警每个自然日最多写一条日志，避免十分钟刷一次。
        self.storage_warned_on = None

        rec_cfg = cfg.get("record") or {}
        self.recorder = Recorder(rec_cfg, self.fps)
        # 本人在电脑前时：keep 照录照推 / mute 照录不推 / skip 不录不推。
        # 旧配置 behavior.skip_when_user_active 仍可推导出同样的语义。
        self.owner_policy = normalize_owner_policy(
            rec_cfg.get("owner_at_computer"),
            legacy_skip=self.legacy_skip_when_active,
        )
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
        # 录像收尾时才知道文件名，推送文案要用事件发生的时刻，先存着
        self.last_event_at: datetime | None = None

        self.cap = None
        self.detector = None
        self.candidate_frame = None
        self.candidate_dets = []

        self.started_at = None
        self.last_frame_at = None
        self.last_heartbeat_at = None
        self.frames = 0
        self.last_spoke_at = None
        self.spoke_this_event = False
        # 有人进来但一开始没正脸时，最多重试找脸多久（秒）。
        self.voice_retry_started_at = None
        self.last_voice_check_at = None
        self.empty_streak = 0
        # 连续这么久没检测到人，才算「这一波人走了」，语音重新武装。
        # 否则人一直站在原地，每 90 秒就会被自己的监控播报一次。
        self.empty_reset_frames = max(5, int(self.fps * 3))
        self.events_today = 0
        self.today = datetime.now().date()
        self.fail_streak = 0

    def open_camera(self) -> bool:
        self.cap = camera.open_capture(
            self.camera_spec,
            backend=self.backend_name,
            width=self.width,
            height=self.height,
            fps=self.fps,
            open_timeout_ms=self.open_timeout_ms,
            read_timeout_ms=self.read_timeout_ms,
        )
        if not self.cap.isOpened():
            log.error(
                "打不开 %s。%s",
                self.camera_spec.label,
                "检查地址、账号密码与网络连通性"
                if self.camera_spec.kind == camera.URL
                else "试试改 config.yaml 里的 camera.index / camera.backend",
            )
            return False
        self.gate.reset()  # 重连后画面尺寸可能变了，丢掉旧参考帧
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        log.info(
            "采集已打开：%s，%sx%s (请求 %sx%s)",
            self.camera_spec.label, actual_w, actual_h, self.width, self.height,
        )
        return True

    def load_model(self) -> None:
        log.info("加载检测器（engine=%s，模型 %s）", self.detector_cfg.get("engine", "auto"),
                 self.model_name)
        try:
            self.detector = build_detector(self.detector_cfg, base_dir=str(paths.PROJECT_ROOT))
        except Exception:
            self.exit_reason = "model"
            self._record_exit(health.EXIT_MODEL, self.exit_reason)
            log.exception("检测器加载失败")
            sys.exit(health.EXIT_MODEL)
        cv2.setNumThreads(max(1, int(self.detector_cfg.get("threads", 2))))
        log.info(
            "检测器就绪：%s%s",
            self.detector.name,
            "（torch 兜底，CPU 占用会明显更高）" if self.detector.name == "torch" else "",
        )

    def detect(self, frame) -> list:
        """跑一次人体检测。引擎细节在 onnx_detector.py。

        推理异常不往外抛：偶发的单帧失败不应该让监控整个退出。但也不能装没事——
        前几次打完整栈，之后每 100 次提醒一次，免得日志被刷满。
        """
        if self.detector is None:
            return []
        try:
            return self.detector.detect(frame)
        except Exception as exc:  # noqa: BLE001
            self.detect_errors += 1
            if self.detect_errors <= 3 or self.detect_errors % 100 == 0:
                log.exception("检测失败（累计 %d 次）：%s", self.detect_errors, exc)
            return []

    def user_is_active(self) -> bool:
        """键鼠是否活跃（人在电脑前）。策略怎么用这个信号由调用方决定。"""
        return idle_seconds_windows() < self.idle_threshold

    def record_allowed(self) -> bool:
        """本人在电脑前时，按策略决定要不要开录。进行中的录像不走这里。"""
        return allow_record(self.owner_policy, self.user_is_active())

    def enqueue_notification(self, items: list[dict]) -> int:
        """把一批通知写进 outbox。数据库故障只降级为「本次没入队」，不拖垮监控。

        可能被主循环调用，也可能被 audio-merge 线程的完成回调调用
        （视频合完音轨后再入队）。SQLite 连接不能跨线程共享，所以后者
        临时开一条属于自己的连接；锁把两条路径的写入串起来。
        """
        if not items:
            return 0
        lock = getattr(self, "_outbox_lock", None)
        owner = getattr(self, "_outbox_thread", None)
        try:
            if owner is None or threading.get_ident() == owner:
                if lock is None:
                    return self.outbox.enqueue_many(items)
                with lock:
                    return self.outbox.enqueue_many(items)
            store = OutboxStore(self.outbox_path)
            try:
                if lock is None:
                    return store.enqueue_many(items)
                with lock:
                    return store.enqueue_many(items)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - 通知层故障不能影响采集
            log.warning("写入通知队列失败: %s", exc)
            return 0

    def notify_allowed(self) -> bool:
        """图片和视频共用同一道闸门：dry-run 不发，本人坐在电脑前按策略不发。"""
        if self.dry_run:
            log.info("dry-run 模式，跳过推送")
            return False
        if not allow_notify(self.owner_policy, self.user_is_active()):
            log.info("检测到你本人正在电脑前，按策略跳过推送")
            return False
        return True

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
        # 录像开始于 ACTION_START，抓拍发生在 ACTION_FIRE，两者天然差几秒。
        # 用录像的文件名做抓拍名，照片和录像才配成一对；没有录像时回退到时间戳。
        if self.current_video:
            path = os.path.splitext(self.current_video)[0] + ".jpg"
        else:
            path = snapshot_path(self.snap_dir, when)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img = self.annotate(frame, dets, when) if self.draw_boxes else frame
        ok = cv2.imwrite(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if not ok:
            raise IOError(f"写图失败: {path}")
        return path

    def _outbox_stats(self) -> dict:
        try:
            return self.outbox.stats()
        except Exception:  # noqa: BLE001 - 状态展示不能影响主循环
            return {}

    def write_status(self, camera_ok: bool = True) -> None:
        """把「我还在跑」写成一个文件，供 gallery 的 /status 读取。

        这是回答「监控还在线吗」最直接的依据：看 last_frame_at 距现在多久。
        """
        now = datetime.now()
        outbox_stats = self._outbox_stats()
        storage = getattr(self, "storage_forecast", None) or {}
        used_bytes = storage.get("used_bytes")
        max_bytes = storage.get("max_bytes")
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
            "camera_source": self.camera_spec.label,
            "engine": getattr(self.detector, "name", None),
            "gated": self.gate.stats(),
            "frames": self.frames,
            "events_today": self.events_today,
            "recording": self.recorder.recording,
            "dry_run": self.dry_run,
            "pending_outbox": outbox_stats.get("pending", 0),
            "last_push_success_at": outbox_stats.get("last_success_at"),
            "notify_channels": len(enabled_channels(getattr(self, "notify_cfg", {}))),
            "storage_status": storage.get("status"),
            "storage_days_left": storage.get("projected_days"),
            "storage_used_gb": (
                round(used_bytes / (1024 ** 3), 2) if used_bytes is not None else None
            ),
            "storage_max_gb": (
                round(max_bytes / (1024 ** 3), 2) if max_bytes is not None else None
            ),
        }
        try:
            health.write_json_atomic(self.status_path, payload)
            if getattr(self, "_status_write_failed", False):
                log.info("状态文件已恢复写入")
                self._status_write_failed = False
        except OSError as exc:
            if not getattr(self, "_status_write_failed", False):
                log.warning("写状态文件失败（主循环继续）: %s", exc)
                self._status_write_failed = True

    def write_health(self) -> None:
        """每秒写一次的健康快照，供 guardian / 界面 / 诊断包读取。"""
        recorder = getattr(self, "recorder", None)
        outbox_stats = self._outbox_stats()
        extra = {
            "recording": bool(getattr(recorder, "recording", False)),
            "camera_ok": bool(getattr(self, "camera_ok", True)),
            "pending_outbox": outbox_stats.get("pending", 0),
            "last_push_success_at": outbox_stats.get("last_success_at"),
            "notify_channels": len(enabled_channels(getattr(self, "notify_cfg", {}))),
        }
        try:
            extra["desired_state"] = control.read_desired_state(self.root)
        except Exception:  # noqa: BLE001 - 状态目录异常不该拖垮主循环
            extra["desired_state"] = "running"
        payload = health.build_health_payload(
            pid=os.getpid(),
            loop_seq=self.loop_seq,
            started_at=self.started_at,
            last_frame_at=self.last_frame_at,
            last_inference_at=self.last_inference_at,
            consecutive_errors=self.consecutive_errors,
            camera_reopens=self.camera_reopens,
            selfcheck=self.selfcheck,
            extra=extra,
        )
        try:
            health.write_health(self.health_path, payload)
            if getattr(self, "_health_write_failed", False):
                log.info("健康快照已恢复写入")
                self._health_write_failed = False
        except OSError as exc:
            if not getattr(self, "_health_write_failed", False):
                log.warning("写健康快照失败（主循环继续）: %s", exc)
                self._health_write_failed = True

    def _dump_stacks(self) -> None:
        fault = getattr(self.diagnostics, "fault_file", None)
        if fault is not None:
            faulthandler.dump_traceback(file=fault, all_threads=True)
        else:
            faulthandler.dump_traceback(all_threads=True)

    def _on_stall(self) -> None:
        log.error("主循环停滞超过 %.0f 秒，退出等待守护进程重启", self.stall_seconds)
        self._record_exit(health.EXIT_STALLED, "stalled")
        os._exit(health.EXIT_STALLED)

    def _watchdog(self) -> None:
        health.watchdog_loop(
            get_seq=lambda: self.loop_seq,
            dump_stacks=self._dump_stacks,
            on_stall=self._on_stall,
            sleep=time.sleep,
            stop_event=self._watchdog_stop,
            threshold_seconds=self.stall_seconds,
        )

    def _exit_code(self) -> int:
        return {
            "config": health.EXIT_CONFIG,
            "camera": health.EXIT_CAMERA,
            "model": health.EXIT_MODEL,
            "unhandled": health.EXIT_UNHANDLED,
            "stalled": health.EXIT_STALLED,
        }.get(self.exit_reason, health.EXIT_OK)

    def _record_exit(self, code: int, reason: str) -> None:
        """优先交给 Diagnostics，避免它注册的 atexit 用 normal 覆盖真实原因。"""
        if self.diagnostics is not None:
            self.diagnostics.note_exit(code, reason)
            return
        try:
            health.write_exit_record(
                self.exit_record_path, pid=os.getpid(), code=code, reason=reason
            )
        except OSError:
            # 退出路径上写文件失败不能反过来把进程搞挂
            pass

    def maybe_speak(
        self, frame, dets: list, now: datetime, *, force_check: bool = False
    ) -> None:
        """有人进入时，对现场的人播报语音提示。

        只在「确认有人」之后调用。require_face 为真时会先找脸，
        找到才开口——对着背影说话没有意义，也容易误伤路过的人。
        force_check=True 用于事件期间的定时重试：不再受每隔 N 帧的限制。
        """
        if not self.speaker.enabled or self.spoke_this_event:
            return

        face_found = False
        if self.require_face:
            # 人脸检测不必每帧都跑，隔几帧一次，省 CPU
            if not force_check and self.frames % self.face_every_n != 0:
                return
            faces = self.face_finder.find(frame, dets)
            face_found = len(faces) > 0

        if not should_speak(
            now, self.last_spoke_at, self.voice_cooldown, face_found, self.require_face
        ):
            return

        if self.volume_mode == "session":
            # 音频会话要等进程第一次发声后才出现，启动时那次多半改不到，
            # 所以每次播报前再试一遍（只动自己那条，不影响用户听歌）。
            audio_device.set_app_session_percent(os.getpid(), 100.0)
        self.speaker.say()
        self.last_spoke_at = now
        self.spoke_this_event = True
        log.info("语音提示已触发（检测到人脸: %s）", "是" if face_found else "否")

    def retry_voice(self, frame, dets: list, now: datetime) -> None:
        """人已经在画面里但还没出声时，定时重试找脸。

        真实场景：人推门进来时背对镜头，走两步才转身。只在触发那几帧
        找脸会错过正脸；这里在事件开始后的 30 秒内每秒重试一次，
        看到脸就播报。仍然遵守冷却、本人静默与 require_face 规则。
        """
        if not dets or self.spoke_this_event:
            return
        if self.is_owner_event() or self.last_identity_label == LABEL_OWNER:
            return
        if self.voice_retry_started_at is None:
            self.voice_retry_started_at = now
        elif (now - self.voice_retry_started_at).total_seconds() > VOICE_RETRY_SECONDS:
            return
        if (self.last_voice_check_at is not None
                and (now - self.last_voice_check_at).total_seconds() < 1.0):
            return
        self.last_voice_check_at = now
        self.maybe_speak(frame, dets, now, force_check=True)

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
        if not self.record_allowed():
            log.info("本人在电脑前，按策略不开始录像")
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
            # 合并完成后才推视频——合并前的 mp4 还没有音轨
            self.audio.merge_async(
                video, wav, pre_roll_delay_ms(pre_frames, self.fps), on_done=self.push_video
            )
        elif video:
            self.push_video(video)

    def push_video(self, video_path: str) -> None:
        """把一段收尾好的录像写进 outbox 的视频慢道。"""
        if not video_path or not os.path.exists(video_path):
            return
        if not self.notify_allowed():
            return
        when = self.last_event_at
        stamp = (when.strftime("%H:%M:%S") if when
                 else os.path.splitext(os.path.basename(video_path))[0])
        mb = os.path.getsize(video_path) / 1048576
        event_id = when.isoformat() if when else datetime.now().isoformat()
        items = build_items(
            self.notify_cfg,
            event_id=event_id,
            text=f"{stamp} 房间录像（{mb:.1f} MB，已压缩）",
            video_path=video_path,
            severity="event",
            owner=self.last_identity_label,
        )
        # text/image 已经由 fire_event 入队；这里只补视频，避免重复文字。
        self.enqueue_notification([item for item in items if item["kind"] == "video"])

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
        self._update_storage_forecast(now)

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
            self._update_storage_forecast(now)

    def _update_storage_forecast(self, now: datetime) -> None:
        """按最近几天的实际写入量预测还能撑多久；预警每天最多说一次。"""
        try:
            files = iter_media_files(self.snap_dir)
        except OSError:
            return
        forecast = storage_forecast(
            files,
            now=now,
            max_bytes=self.max_total_bytes,
            keep_days=self.keep_days,
            warn_days=self.storage_warn_days,
        )
        self.storage_forecast = forecast
        if forecast["status"] not in (STORAGE_WARNING, STORAGE_OVER):
            return
        if self.storage_warned_on == now.date():
            return
        self.storage_warned_on = now.date()
        if forecast["status"] == STORAGE_OVER:
            log.warning(
                "存储预警：已用 %.2f GB，超过上限 %.2f GB，正在按最旧优先清理",
                forecast["used_bytes"] / (1024 ** 3),
                forecast["max_bytes"] / (1024 ** 3),
            )
        else:
            log.warning(
                "存储预警：已用 %.2f GB，按最近增速约 %.1f 天后达到上限 %.2f GB"
                "（可调 storage.max_total_gb / snapshot.keep_days）",
                forecast["used_bytes"] / (1024 ** 3),
                forecast["days_to_cap"] or 0.0,
                forecast["max_bytes"] / (1024 ** 3),
            )

    def maybe_heartbeat(self, now: datetime) -> None:
        """定时报平安。收不到心跳，就说明它挂了。"""
        if not should_heartbeat(now, self.last_heartbeat_at, self.heartbeat_minutes):
            return

        if self.dry_run:
            log.info("[心跳-仅日志] 监控在线 %s，今日抓拍 %d 次",
                     humanize_duration((now - self.started_at).total_seconds()),
                     self.events_today)
            self.last_heartbeat_at = now
            return

        text = (
            f"监控在线 · 已运行 {humanize_duration((now - self.started_at).total_seconds())}\n"
            f"今日抓拍 {self.events_today} 次 · 最后画面 "
            f"{self.last_frame_at.strftime('%H:%M:%S') if self.last_frame_at else '未知'}"
        )
        # 走 outbox：断网时这条报平安也应该补发，而不是直接消失。
        items = build_items(
            self.notify_cfg,
            event_id=f"digest:{now.date().isoformat()}",
            text=text,
            severity="digest",
        )
        if not items or self.enqueue_notification(items):
            # 没有渠道也算处理过，避免每个循环重复尝试；入队失败则保持到期，下轮重试。
            self.last_heartbeat_at = now

    def fire_event(self, frame, dets: list, when: datetime) -> None:
        conf = max((d.conf for d in dets), default=0.0)
        path = self.save_snapshot(frame, dets, when)
        self.events_today += 1
        log.info("事件已抓拍: %s (置信度 %.2f)，今日第 %d 次", path, conf, self.events_today)
        if self.current_video:
            log.info("配套录像: %s", self.current_video)

        self.last_event_at = when
        notified = False
        if self.notify_allowed():
            text = (f"{when.strftime('%H:%M:%S')} 检测到有人进入房间"
                    f"（{identity_text(self.last_identity_label)}）\n存档: {path}")
            if self.current_video:
                text += f"\n录像: {self.current_video}"
            notified = self.enqueue_notification(build_items(
                self.notify_cfg,
                event_id=when.isoformat(),
                text=text,
                image_path=path,
                severity="event",
                owner=self.last_identity_label,
            )) > 0

        self.store.add(when, path, conf, notified, video=self.current_video,
                       identity=self.last_identity_label)

    def run(self) -> None:
        results = selfcheck_mod.run(self.cfg, self.root)
        self.selfcheck = [
            {"name": item.name, "ok": item.ok, "detail": item.detail}
            for item in results
        ]
        for item in results:
            if not item.ok:
                log.warning("启动自检：%s 未通过（%s）", item.name, item.detail)

        if not self.open_camera():
            self.exit_reason = "camera"
            self._record_exit(health.EXIT_CAMERA, self.exit_reason)
            sys.exit(health.EXIT_CAMERA)
        self.load_model()

        os.makedirs(self.snap_dir, exist_ok=True)
        prune_old(self.snap_dir, self.keep_days, datetime.now())
        log.info("开始监控。Ctrl+C 退出。")

        self.started_at = datetime.now()
        self._watchdog_stop.clear()
        threading.Thread(
            target=self._watchdog, daemon=True, name="health-watchdog"
        ).start()
        self.delivery_fast.start()
        self.delivery_video.start()
        self.write_health()
        interval = 1.0 / max(1, self.fps)
        last_status_write = 0.0
        last_health_write = 0.0
        last_stop_check = 0.0

        # 上一轮如果是被强杀的，master 档抬高过的主音量还没还原，这里补上
        restored = audio_device.restore_backup(self.volume_backup)
        if restored:
            log.info("系统音量：%s", restored)
        # 上一轮残留的停止请求必须先清掉，否则新进程一起来就自己退了
        control.clear_stop_request(self.root)
        log.info("系统音量：%s", self.volume_guard.apply())
        if self.volume_guard.changed:
            log.info("提示：这段音量改动会在监控退出时还原（voice.restore_system_volume）")

        try:
            while True:
                self.loop_seq += 1
                # 停止请求：界面/`rw stop` 写文件，这里每秒看一次。
                # 走 break 而不是强杀，才能让 finally 把 mp4 收尾、音轨合上。
                if time.time() - last_stop_check >= 1.0:
                    last_stop_check = time.time()
                    if control.stop_requested(self.root, os.getpid()):
                        log.info("收到停止请求，正在收尾（录像/音轨/音量）…")
                        self.exit_reason = "stop_requested"
                        break

                # 按整轮耗时控速，而不是无条件 sleep：RTSP 的 read() 本身就会阻塞到
                # 下一帧，再补 100ms 会把实际帧率压到 8fps 以下。
                loop_start = time.monotonic()
                ok, frame = self.cap.read()
                now = datetime.now()

                if not ok:
                    # 摄像头被占用、驱动抽风、USB 掉线、RTSP 断流都会走到这里
                    self.fail_streak += 1
                    self.consecutive_errors += 1
                    self.camera_ok = False
                    log.warning("读帧失败（连续 %d 次）", self.fail_streak)
                    self.write_status(camera_ok=False)
                    # 文件源没有「断线重连」这回事，读完就该退出，
                    # 否则会一直重开、把最后一帧当成永远失败的摄像头。
                    if self.camera_spec.kind == camera.FILE and self.fail_streak >= 2:
                        log.info("视频文件已播完，退出")
                        self.exit_reason = "file_end"
                        break
                    if self.fail_streak >= self.reconnect_after:
                        self.reconnect_attempt += 1
                        self.camera_reopens += 1
                        delay = camera.backoff_seconds(
                            self.reconnect_attempt, self.reconnect_base, self.reconnect_max
                        )
                        log.error(
                            "连续失败 %d 次，%.0f 秒后重连（第 %d 次）",
                            self.fail_streak, delay, self.reconnect_attempt,
                        )
                        self.cap.release()
                        time.sleep(delay)
                        if self.open_camera():
                            log.info("采集重连成功")
                            self.fail_streak = 0
                            self.reconnect_attempt = 0
                            self.camera_ok = True
                        else:
                            self.fail_streak = 0  # 下一轮重新计数，避免原地死循环
                    else:
                        time.sleep(0.2)
                    continue

                self.fail_streak = 0
                self.reconnect_attempt = 0
                self.consecutive_errors = 0
                self.camera_ok = True
                self.frames += 1
                self.last_frame_at = now

                if now.date() != self.today:
                    self.today = now.date()
                    self.events_today = 0
                    prune_old(self.snap_dir, self.keep_days, now)

                # 静止门控：空房间不推理，有人走动才唤醒。
                # busy 保证「正在录像 / 刚有人经过」期间全速推理，事件一帧都不省。
                mono = time.monotonic()
                busy = self.recorder.recording or (
                    self.last_detection_at is not None
                    and (mono - self.last_detection_at) < self.gate.idle_grace
                )
                if self.gate.should_infer(frame, mono, busy):
                    dets = self.detect(frame)
                    self.last_inference_at = now
                else:
                    dets = []
                if dets:
                    self.last_detection_at = mono
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
                        self.voice_retry_started_at = None
                        self.last_voice_check_at = None
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

                self.retry_voice(frame, dets, now)
                self.pump_recording(frame, dets, now)
                self.enforce_storage(now)
                self.maybe_heartbeat(now)

                # 状态文件没必要每帧都写，5 秒一次足够回答「还活着吗」
                if time.time() - last_status_write >= 5.0:
                    self.write_status(camera_ok=True)
                    last_status_write = time.time()

                # 健康快照默认每秒一次，供 guardian / 界面 / 诊断包读取
                if time.time() - last_health_write >= self.health_interval:
                    self.write_health()
                    last_health_write = time.time()

                spent = time.monotonic() - loop_start
                if spent < interval:
                    time.sleep(interval - spent)
        except KeyboardInterrupt:
            self.exit_reason = "keyboard"
            log.info("收到 Ctrl+C，退出")
        except BaseException:
            self.exit_reason = "unhandled"
            raise
        finally:
            self._watchdog_stop.set()
            self.delivery_fast.stop()
            self.delivery_video.stop()
            self._record_exit(self._exit_code(), self.exit_reason)
            logutil.event(log, "watcher_exit",
                          code=self._exit_code(), reason=self.exit_reason)
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
            try:
                self.outbox.close()
            except Exception:  # noqa: BLE001 - 退出路径上不抛
                pass

    def probe(self) -> None:
        """打开摄像头拍一张 preview.jpg，用来确认画面角度对不对。

        `probe_json=True`（命令行 `rw probe --json`）时只多输出**一行** JSON：
        成功给尺寸与耗时，失败给 error。界面靠它知道「到底成没成」——
        0.1.7 只把输出丢进 probe.log，用户看不到任何反馈。

        JSON 一律 `ensure_ascii=True`：中文 Windows 的控制台是 GBK，
        直接输出中文可能抛 UnicodeEncodeError，把成功也变成失败。
        """
        started = time.monotonic()
        if not self.open_camera():
            self._probe_fail("打不开采集源（被别的程序占用 / 编号不对 / 地址或密码错误）", 2)
            return

        ok, frame = self.cap.read()
        self.cap.release()
        if not ok:
            self._probe_fail("设备已连上，但读不到画面", 3)
            return

        os.makedirs(paths.MEDIA_DIR, exist_ok=True)
        out = paths.MEDIA_DIR / "preview.jpg"
        if not cv2.imwrite(str(out), frame):
            self._probe_fail(f"写文件失败：{out}", 3)
            return

        height, width = frame.shape[:2]
        ms = int((time.monotonic() - started) * 1000)
        if self.probe_json:
            print(json.dumps({"ok": True, "path": str(out), "width": width,
                              "height": height, "ms": ms, "error": ""}))
        log.info("已保存 %s（%sx%s，%d 毫秒），打开看看角度对不对", out, width, height, ms)

    def _probe_fail(self, message: str, code: int) -> None:
        log.error("%s", message)
        if self.probe_json:
            print(json.dumps({"ok": False, "path": "", "width": 0, "height": 0,
                              "ms": 0, "error": message}))
        sys.exit(code)


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
    best_frame = None
    best_row = None
    best_area = 0.0

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
                area = float((hit.row[2] - hit.row[0]) * (hit.row[3] - hit.row[1]))
                if area > best_area:
                    best_area, best_frame, best_row = area, img, hit.row
        if not feats:
            print("这张图里没提取到可用的人脸特征（可能太小/太糊/侧脸）")
            return 3
    else:
        cam = cfg.get("camera") or {}
        # 注册也走同一套采集源解析：接 IP 摄像头时能远程取画面注册，
        # 不用把笔记本搬到摄像头跟前。
        try:
            spec = camera.parse_source(cam.get("source"), int(cam.get("index", 0)))
        except ValueError as exc:
            print(exc)
            return 2
        cap = camera.open_capture(
            spec,
            backend=str(cam.get("backend", "dshow")),
            width=int(cam.get("width", 1280)),
            height=int(cam.get("height", 720)),
            fps=int(cam.get("fps", 10)),
        )
        if not cap.isOpened():
            print(f"打不开 {spec.label}，注册失败")
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
                        area = float(
                            (hits[0].row[2] - hits[0].row[0])
                            * (hits[0].row[3] - hits[0].row[1])
                        )
                        if area > best_area:
                            best_area, best_frame, best_row = area, frame.copy(), hits[0].row
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

    if not idn.save_owner(feats, extra={"created_at": datetime.now().isoformat(timespec="seconds")}):
        return 4
    if best_frame is not None and best_row is not None:
        photo = save_owner_photo(idn.owner_file, best_frame, best_row)
        if photo:
            print(f"已保存本人照片：{photo}")
    print(f"注册完成：{idn.owner_file}（{len(feats)} 个样本，阈值 {idn.threshold}）")
    print("接下来可以验证一下：rw whoami <某张你自己的截图>")
    return 0


def whoami(cfg: dict, image_path: str) -> int:
    """测试识别：这张图里的人是不是你。"""
    idn = Identity(cfg.get("identity") or {})
    if not idn.usable:
        print("识别不可用：模型缺失，或还没注册本人（先跑 rw enroll）")
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


def print_storage(config_path: str, as_json: bool = False) -> None:
    """看存储占用、上限、容量预测，以及最旧/最新文件。"""
    cfg = load_config(config_path)
    snap = cfg.get("snapshot") or {}
    directory = snap.get("dir") or resolve("data/captures")
    st = cfg.get("storage") or {}
    max_gb = float(st.get("max_total_gb", 20))
    min_free_gb = float(st.get("min_free_gb", 3))
    keep_days = int(snap.get("keep_days", 30))
    warn_days = float(st.get("warn_days", 7))
    max_bytes = int(max_gb * (1024 ** 3))

    try:
        free = shutil.disk_usage(directory).free if os.path.isdir(directory) else None
    except OSError:
        free = None

    summary = storage_summary(
        directory,
        max_bytes=max_bytes,
        keep_days=keep_days,
        warn_days=warn_days,
        free_bytes=free,
    )
    if as_json:
        print(json.dumps(summary, ensure_ascii=False))
        return

    if not os.path.isdir(directory):
        print(f"存档目录还不存在：{directory}")
        return

    forecast = summary["forecast"]
    files = iter_media_files(directory)
    total = summary["used_bytes"]

    print("=== 存储占用 ===")
    print(f"目录        : {os.path.abspath(directory)}")
    print(f"媒体文件    : {len(files)} 个")
    print(f"当前占用    : {total / (1024 ** 3):.2f} GB")
    print(f"容量上限    : {max_gb:.0f} GB" + ("（0 = 不限）" if max_gb <= 0 else ""))
    print(f"按天保留    : {keep_days} 天" + ("（0 = 不限）" if keep_days <= 0 else ""))
    if free is not None:
        print(f"磁盘剩余    : {free / (1024 ** 3):.2f} GB（低于 {min_free_gb:.0f} GB 会逼近清理）")

    daily_mb = forecast["daily_bytes"] / (1024 ** 2)
    print(f"最近增速    : {daily_mb:.1f} MB/天（按最近完整自然日估算）")
    if forecast["projected_days"] is None:
        print("预计可用    : 不受容量或保留期限制")
    else:
        print(f"预计可用    : {forecast['projected_days']:.1f} 天"
              "（取容量上限与保留期中较早到的那个）")

    status = forecast["status"]
    if status == "over":
        print("状态        : ⚠ 已超过容量上限，下一轮检查会删掉最旧的文件")
    elif status == "warning":
        days = forecast["days_to_cap"]
        print(f"状态        : ⚠ 按当前增速约 {days:.1f} 天后达到容量上限"
              "（可调大 storage.max_total_gb 或调小 snapshot.keep_days）")
    elif status == "unlimited":
        print("状态        : 不限容量与天数（磁盘由你自行管理）")
    else:
        print("状态        : 正常")

    if files:
        def fmt(ts: float) -> str:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        oldest = min(files, key=lambda x: x[0])
        newest = max(files, key=lambda x: x[0])
        print(f"最旧文件    : {fmt(oldest[0])}  {oldest[1]}")
        print(f"最新文件    : {fmt(newest[0])}  {newest[1]}")

        by_ext: dict = {}
        for _m, path, size in files:
            ext = os.path.splitext(path)[1].lower()
            cnt, sz = by_ext.get(ext, (0, 0))
            by_ext[ext] = (cnt + 1, sz + size)
        print("按类型      :")
        for ext, (cnt, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1]):
            print(f"  {ext:<6s} {cnt:5d} 个   {size / (1024 ** 2):9.1f} MB")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="房间进入检测与抓拍")
    parser.add_argument("--config", default=resolve("config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="抓拍但不推送")
    parser.add_argument("--probe", action="store_true", help="只拍一张预览图然后退出")
    parser.add_argument("--json", action="store_true",
                        help="配合 --probe / --storage：输出一行 JSON（界面用）")
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
    args = parser.parse_args(argv)

    handlers = [logging.StreamHandler()]
    if args.log:
        handler = logutil.safe_rotating_file_handler(args.log)
        if handler is not None:
            handlers.append(handler)
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
        print_storage(args.config, as_json=args.json)
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

    diagnostics = None
    if not args.probe:
        health_cfg = cfg.get("health") or {}
        diagnostics = Diagnostics(
            resolve(health_cfg.get("fault_log"), "var/logs/watcher-fault.log"),
            resolve(health_cfg.get("exit_record"), "var/state/last_exit.json"),
        )
        diagnostics.install()

    try:
        w = Watcher(cfg, dry_run=args.dry_run, diagnostics=diagnostics)
    except SystemExit as exc:
        if diagnostics is not None:
            diagnostics.note_exit(int(exc.code or 0), "config")
        raise
    w.probe_json = bool(args.json)
    if args.probe:
        w.probe()
    else:
        w.run()


def print_status(config_path: str) -> None:
    """读 watcher 写下的 status.json，判断它是否还活着。"""
    cfg = load_config(config_path)
    path = (cfg.get("storage") or {}).get("status") or resolve("var/state/status.json")

    desired = control.read_desired_state(paths.PROJECT_ROOT)
    print(f"期望状态    : {'运行' if desired == 'running' else '已暂停'}")
    exit_rec = health.read_exit_record(
        resolve((cfg.get("health") or {}).get("exit_record"), "var/state/last_exit.json")
    )
    if exit_rec:
        print(f"上次退出    : {exit_rec.get('reason')}（退出码 {exit_rec.get('code')}）")

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
