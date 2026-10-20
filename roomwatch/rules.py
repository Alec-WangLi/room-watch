"""纯逻辑层：不依赖摄像头、不依赖网络，可脱离硬件单测。

这里放的是整个 watcher 里最容易写错、也最值得测试的部分：
1. 什么时候算「有人进入」（连续帧确认，避免单帧误检）
2. 从进入的那段时间里挑「最好的一张」作为抓拍
3. 冷却窗口，防止一个人在房间里待 20 分钟把手机炸掉
4. 快照归档路径与过期清理
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence, Tuple

# 观测一帧之后，调用方应该做什么
ACTION_NONE = "none"    # 什么都不做
ACTION_START = "start"  # 首次确认有人，把当前帧存为候选
ACTION_BEST = "best"    # 当前帧比候选更好，替换候选
ACTION_FIRE = "fire"    # 触发事件，用候选帧出图


@dataclass(frozen=True)
class Detection:
    """一个检测到的人形框，坐标是像素。"""
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


@dataclass
class TriggerState:
    """把「连续 N 帧有人 → 挑最好的一帧 → 冷却」这段状态机封在这里。"""

    trigger_frames: int = 3        # 连续多少帧有人，才认定是「进入」而不是误检
    capture_window: int = 8        # 确认进入后，最多再观察多少帧来挑最好的一张
    cooldown_seconds: float = 90.0  # 一次事件后多久内不再报
    min_conf: float = 0.5

    # 运行状态
    streak: int = 0
    armed: bool = False
    frames_since_arm: int = 0
    last_event_at: Optional[datetime] = None
    best_score: float = -1.0

    def reset(self) -> None:
        self.streak = 0
        self.armed = False
        self.frames_since_arm = 0
        self.best_score = -1.0

    def in_cooldown(self, now: datetime) -> bool:
        if self.last_event_at is None:
            return False
        return now - self.last_event_at < timedelta(seconds=self.cooldown_seconds)

    def observe(
        self,
        detections: Sequence[Detection],
        now: datetime,
        frame_size: Tuple[int, int],
    ) -> str:
        """喂一帧进去，返回调用方该执行的动作。"""
        if self.in_cooldown(now):
            return ACTION_NONE

        person = self._pick_person(detections, self.min_conf)
        if person is None:
            self.streak = 0
            # 人走了：如果之前已进入采集窗口，就用攒下的最好那张出图
            if self.armed:
                self.armed = False
                self.frames_since_arm = 0
                self.best_score = -1.0
                self.last_event_at = now
                return ACTION_FIRE
            return ACTION_NONE

        self.streak += 1

        if not self.armed:
            if self.streak < self.trigger_frames:
                return ACTION_NONE
            self.armed = True
            self.frames_since_arm = 0
            self.best_score = frame_score(person, frame_size)
            return ACTION_START

        score = frame_score(person, frame_size)
        self.frames_since_arm += 1
        if score > self.best_score:
            self.best_score = score
            return ACTION_BEST

        # 人一直在，采集窗口也满了，别再等了
        if self.frames_since_arm >= self.capture_window:
            self.reset()
            self.last_event_at = now
            return ACTION_FIRE

        return ACTION_NONE

    @staticmethod
    def _pick_person(
        detections: Sequence[Detection], min_conf: float
    ) -> Optional[Detection]:
        candidates = [d for d in detections if d.conf >= min_conf]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.area)


def frame_score(det: Detection, frame_size: Tuple[int, int]) -> float:
    """给一帧打「适不适合当存档照」的分。

    三个直觉：人越大越好、越靠画面中间越好、贴边（半个身子在画外）要扣分。
    """
    w, h = frame_size
    if w <= 0 or h <= 0:
        return 0.0

    area_norm = det.area / float(w * h)
    cx, cy = det.center
    offset = (abs(cx - w / 2.0) / (w / 2.0) + abs(cy - h / 2.0) / (h / 2.0)) / 2.0
    central = 1.0 - 0.5 * min(1.0, offset)

    margin = 2.0
    touches_edge = (
        det.x1 <= margin
        or det.y1 <= margin
        or det.x2 >= w - margin
        or det.y2 >= h - margin
    )
    edge_penalty = 0.55 if touches_edge else 1.0

    return area_norm * central * edge_penalty


def should_heartbeat(
    now: datetime,
    last_at: Optional[datetime],
    interval_minutes: float,
) -> bool:
    """到点该发「我还活着」了吗？

    这是典型的 dead man's switch：你不是靠心跳本身确认正常，
    而是靠「该来的心跳没来」判断出了问题。
    """
    if interval_minutes <= 0:
        return False
    if last_at is None:
        return True
    return now - last_at >= timedelta(minutes=interval_minutes)


def should_speak(
    now: datetime,
    last_spoke_at: Optional[datetime],
    cooldown_seconds: float,
    face_found: bool,
    require_face: bool,
) -> bool:
    """该开口说话了吗？

    require_face=True 时，没看到脸就不出声——避免对着一个背影或者
    路过的半个人说话。冷却窗口则保证不会同一个人反复播报。
    """
    if require_face and not face_found:
        return False
    if last_spoke_at is not None:
        if now - last_spoke_at < timedelta(seconds=cooldown_seconds):
            return False
    return True


# check() 的返回值。空串表示「继续录」，非空就是要停，同时它本身就是日志原因。
RECORD_CONTINUE = ""
RECORD_STOP_IDLE = "人已离开画面"
RECORD_STOP_MAX = "达到最长录像时间"


@dataclass
class RecordPolicy:
    """一段录像该录到什么时候为止。

    三个约束互相拉扯，这里把它们摆平：

    1. 人还在就别停 —— 保洁收拾 20 分钟，你不想只看开头那 3 分钟
    2. 人走了就别磨 —— 对着空房间录像纯属浪费磁盘，事后翻找也更痛苦
    3. 再怎么样也有硬上限 —— 忘了拔摄像头也不该一天写出几十 GB

    min_seconds 的作用是「地板」：小於它就算画面空了也继续录。
    因为 YOLO 偶尔有一两帧漏检，人在画面里站着却被判成「走了」，
    没有这个地板，录像会在人还没走时被硬生生掐断。
    """

    min_seconds: float = 20.0         # 地板：至少录这么久
    idle_stop_seconds: float = 15.0   # 人离开画面这么久就收尾
    max_seconds: float = 300.0        # 天花板：单段到此为止，之后自动接下一段
    max_total_seconds: float = 1800.0  # 一整次在场的总上限，防止有人待一天写满磁盘

    # 运行状态
    started_at: Optional[datetime] = None
    last_person_at: Optional[datetime] = None
    total_recorded: float = 0.0       # 已在本次在场里录了多少秒（跨段累计）

    @property
    def active(self) -> bool:
        return self.started_at is not None

    @property
    def exhausted(self) -> bool:
        """总额度用完没有。到了就不再续下一段——避免人在房间里赖一天写满磁盘。"""
        return self.total_recorded >= self.max_total_seconds

    def begin(self, now: datetime, has_person: bool = True, continuing: bool = False) -> None:
        """开录。

        continuing=True 表示这是上一段到达上限后的接力，
        累计时长要接着算；否则是一次新的在场，清零。
        """
        if not continuing:
            self.total_recorded = 0.0
        self.started_at = now
        self.last_person_at = now if has_person else None

    def note(self, now: datetime, has_person: bool) -> None:
        """每帧告知当前有没有人，用来维护「最后一次看见人」的时刻。"""
        if self.started_at is None:
            return
        if has_person:
            self.last_person_at = now

    def elapsed(self, now: datetime) -> float:
        if self.started_at is None:
            return 0.0
        return (now - self.started_at).total_seconds()

    def idle_for(self, now: datetime) -> float:
        """画面里已经多久没见到人了。一次都没见到时按「从头算」处理。"""
        if self.started_at is None:
            return 0.0
        reference = self.last_person_at or self.started_at
        return (now - reference).total_seconds()

    def check(self, now: datetime) -> str:
        """该停了吗？返回空串继续，否则返回停止原因。"""
        if self.started_at is None:
            return RECORD_CONTINUE

        # 天花板优先：它必须压得住其它所有理由，否则一旦有人在就不可能停得下来
        if self.elapsed(now) >= self.max_seconds:
            return RECORD_STOP_MAX

        # 还没录够地板时长，就算画面暂时空着也守住，避免把连续的一段切成碎片
        if self.elapsed(now) < self.min_seconds:
            return RECORD_CONTINUE

        if self.idle_for(now) >= self.idle_stop_seconds:
            return RECORD_STOP_IDLE

        return RECORD_CONTINUE

    def finish(self, now: datetime) -> float:
        """正常收尾：把这一段记入累计额度并返回本段时长。"""
        seconds = self.elapsed(now)
        self.total_recorded += seconds
        self.end()
        return seconds

    def end(self) -> None:
        """收尾，回到可重用的初始态（累计额度保留，供判断是否还能续录）。"""
        self.started_at = None
        self.last_person_at = None


def humanize_duration(seconds: float) -> str:
    """把秒数说成人话：1h23m / 45m / 30s"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds % 60:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def gap_seconds(
    last_frame_at: Optional[str | datetime], now: Optional[datetime] = None
) -> Optional[float]:
    """把状态文件里的 ISO 时间字符串，换算成「距现在多少秒」。

    读状态的一方必须自己算这个，不能信文件里存好的 stale 布尔值。
    """
    if last_frame_at is None:
        return None
    if isinstance(last_frame_at, datetime):
        then = last_frame_at
    else:
        try:
            then = datetime.fromisoformat(last_frame_at)
        except (TypeError, ValueError):
            return None
    return ((now or datetime.now()) - then).total_seconds()


def is_stale(last_frame_at: Optional[datetime], now: datetime, tolerance_seconds: float) -> bool:
    """最后一次成功读帧距现在多久了——超过容差就认为监控已经不在线。"""
    if last_frame_at is None:
        return True
    return (now - last_frame_at).total_seconds() > tolerance_seconds


def snapshot_path(base_dir: str, when: datetime) -> str:
    """按 日期目录/完整时间戳.jpg 归档。

    文件名带完整日期：相册里一眼能看出是哪天几点，按名字排序也天然是
    时间顺序。日期目录保留，方便按天清理。
    """
    day = when.strftime("%Y-%m-%d")
    name = when.strftime("%Y-%m-%d_%H%M%S") + ".jpg"
    return os.path.join(base_dir, day, name)


def prune_old(base_dir: str, keep_days: int, now: datetime) -> list:
    """删掉超过保留期的日期目录，返回被删掉的目录列表。"""
    if keep_days <= 0 or not os.path.isdir(base_dir):
        return []

    cutoff = (now - timedelta(days=keep_days)).date()
    removed = []
    for entry in os.listdir(base_dir):
        path = os.path.join(base_dir, entry)
        if not os.path.isdir(path):
            continue
        try:
            day = datetime.strptime(entry, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        for root, _dirs, files in os.walk(path, topdown=False):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass
        try:
            os.rmdir(path)
            removed.append(path)
        except OSError:
            pass
    return removed


# 删容量时要认的文件类型。只删自己产出的东西，别的文件一概不碰。
MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".mp4", ".avi", ".wav")


def iter_media_files(base_dir: str) -> list:
    """列出所有媒体文件，返回 [(mtime, path, size)]。

    用量统计和容量清理共用同一份枚举，避免两处口径不一导致的
    「明明超了却说没超」。
    """
    out: list = []
    if not os.path.isdir(base_dir):
        return out
    for root, _dirs, files in os.walk(base_dir):
        for name in files:
            if not name.lower().endswith(MEDIA_EXTS):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((st.st_mtime, path, st.st_size))
    return out


def usage_bytes(base_dir: str) -> int:
    """当前占用了多少字节。"""
    return sum(size for _m, _p, size in iter_media_files(base_dir))


# 存储预测的四种状态。UI 与日志共用同一套值，避免各写各的中文。
STORAGE_UNLIMITED = "unlimited"   # 不限容量也不限天数
STORAGE_OK = "ok"
STORAGE_WARNING = "warning"       # 按当前增速，很快会碰到上限
STORAGE_OVER = "over"             # 已经超过上限（清理还没跟上）

# 本人在电脑前时，录像与推送各自怎么处理。
OWNER_KEEP = "keep"   # 照录照推
OWNER_MUTE = "mute"   # 照录不推（0.1.16 及以前的默认行为）
OWNER_SKIP = "skip"   # 不录不推
OWNER_POLICIES = (OWNER_KEEP, OWNER_MUTE, OWNER_SKIP)


def normalize_owner_policy(raw, legacy_skip=None) -> str:
    """把配置里的策略值收成一个已知值，并兼容旧的布尔开关。

    `behavior.skip_when_user_active` 只表达「要不要推送」：
    true 等价于 mute，false 等价于 keep。新配置优先，缺失时才看旧键。
    """
    text = str(raw or "").strip().lower()
    if text in OWNER_POLICIES:
        return text
    if legacy_skip is None:
        return OWNER_MUTE
    return OWNER_MUTE if bool(legacy_skip) else OWNER_KEEP


def allow_record(policy: str, user_active: bool) -> bool:
    """当前这一帧允许开录吗？策略只在「人确实在电脑前」时生效。"""
    return not (policy == OWNER_SKIP and user_active)


def allow_notify(policy: str, user_active: bool) -> bool:
    """当前这一帧允许推送吗？"""
    return policy == OWNER_KEEP or not user_active


class OwnerInferenceThrottle:
    """本人在场、又没在录像时，把推理降到每秒一两次。

    MotionGate 的 busy 规则是「刚检测到人就全速」，但人（通常就是本人）
    一直坐在画面里，每帧检测都会把 busy 续期，门控永远轮不到生效，
    YOLO 于是每帧都跑。这里只在身份已确认是本人、画面里确实有人、
    且没有在录像时降频；其余情况一律全速，宁可多算不可漏报。
    """

    def __init__(self, interval_seconds: float = 0.5) -> None:
        self.interval = max(0.0, float(interval_seconds))

    def should_throttle(
        self,
        now: float,
        last_infer_at: float,
        *,
        owner_present: bool,
        person_present: bool,
        recording: bool,
    ) -> bool:
        """now / last_infer_at 用单调时钟秒（time.monotonic）。"""
        if self.interval <= 0 or recording or not owner_present or not person_present:
            return False
        return (now - last_infer_at) < self.interval


def storage_forecast(
    files: Iterable[Tuple[float, str, int]],
    now: Optional[datetime] = None,
    max_bytes: int = 0,
    keep_days: int = 0,
    warn_days: float = 7.0,
    window_days: int = 7,
) -> dict:
    """根据媒体文件的修改时间估计「按当前速度还能撑多久」。

    为什么增长率不含今天：今天的量是半天/小半天的累计，直接拿它当一整天的
    增长率会把预警吹得虚高。先取最近 window_days 天里**完整**自然日的平均；
    完全没有完整数据（刚装的机器）才退回今天的累计量——那是个下限，不是方向。

    容量预警只看 `days_to_cap`：`keep_days` 是用户主动选择的保留期，
    它比 warn_days 短不代表出问题，只在展示「预计保留多久」时参与取小。
    """
    now = now or datetime.now()
    files = list(files)
    used = sum(size for _m, _p, size in files)

    per_day: dict = {}
    oldest = newest = None
    for mtime, _path, size in files:
        day = datetime.fromtimestamp(mtime).date()
        per_day[day] = per_day.get(day, 0) + size
        if oldest is None or mtime < oldest:
            oldest = mtime
        if newest is None or mtime > newest:
            newest = mtime

    today = now.date()
    complete = []
    for offset in range(1, max(1, int(window_days)) + 1):
        day = today - timedelta(days=offset)
        if day in per_day:
            complete.append(per_day[day])
    daily = int(sum(complete) / len(complete)) if complete else int(per_day.get(today, 0))

    days_to_cap: Optional[float] = None
    if max_bytes > 0:
        if used >= max_bytes:
            days_to_cap = 0.0
        elif daily > 0:
            days_to_cap = round((max_bytes - used) / daily, 1)

    days_to_keep = float(keep_days) if keep_days > 0 else None
    candidates = [d for d in (days_to_cap, days_to_keep) if d is not None]
    projected = min(candidates) if candidates else None

    if max_bytes > 0 and used >= max_bytes:
        status = STORAGE_OVER
    elif max_bytes > 0 and days_to_cap is not None and days_to_cap < warn_days:
        status = STORAGE_WARNING
    elif max_bytes <= 0 and keep_days <= 0:
        status = STORAGE_UNLIMITED
    else:
        status = STORAGE_OK

    return {
        "status": status,
        "used_bytes": used,
        "file_count": len(files),
        "daily_bytes": daily,
        "max_bytes": max_bytes,
        "keep_days": keep_days,
        "warn_days": warn_days,
        "window_days": int(window_days),
        "days_to_cap": days_to_cap,
        "days_to_keep_expiry": days_to_keep,
        "projected_days": projected,
        "oldest_at": (
            datetime.fromtimestamp(oldest).isoformat(timespec="seconds")
            if oldest is not None else None
        ),
        "newest_at": (
            datetime.fromtimestamp(newest).isoformat(timespec="seconds")
            if newest is not None else None
        ),
    }



def storage_summary(
    directory,
    *,
    max_bytes: int = 0,
    keep_days: int = 0,
    warn_days: float = 7.0,
    free_bytes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """目录级存储摘要：`rw storage`、设置页与主界面共用同一份口径。"""
    files = iter_media_files(str(directory))
    forecast = storage_forecast(
        files, now=now, max_bytes=max_bytes, keep_days=keep_days, warn_days=warn_days
    )
    return {
        "directory": str(directory),
        "used_bytes": forecast["used_bytes"],
        "max_bytes": max_bytes,
        "free_bytes": free_bytes,
        "keep_days": keep_days,
        "warn_days": warn_days,
        "forecast": forecast,
    }


def _drop_empty_day_dirs(base_dir: str) -> None:
    """删完文件后把空的日期目录收掉，否则相册里会留一堆空壳。"""
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return
    for entry in entries:
        path = os.path.join(base_dir, entry)
        if not os.path.isdir(path):
            continue
        try:
            if not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass


def prune_by_size(
    base_dir: str,
    max_bytes: int,
    now: Optional[datetime] = None,
    protect_seconds: float = 300.0,
) -> list:
    """超过容量上限就按「最旧优先」删除，直到用量回到上限内。

    为什么按 mtime 而不是文件名：文件名是人写的，可能被改；
    mtime 是文件系统记的，动了就是动了。

    protect_seconds 保护的是**正在写入的文件**：刚录的一段 mp4 的 mtime
    就是这一刻，如果它同时又是「最旧的」候选（理论上不会，但目录时间戳可能
    因为拷贝/恢复而异常），删了会毁掉正在录的证据。
    宁可这一轮没清够、下一轮再清，也不能动它。
    """
    if max_bytes <= 0 or not os.path.isdir(base_dir):
        return []

    now_ts = (now or datetime.now()).timestamp()
    files = iter_media_files(base_dir)
    total = sum(size for _m, _p, size in files)
    if total <= max_bytes:
        return []

    removed: list = []
    for mtime, path, size in sorted(files, key=lambda x: x[0]):
        if total <= max_bytes:
            break
        if protect_seconds > 0 and (now_ts - mtime) < protect_seconds:
            continue
        try:
            os.remove(path)
        except OSError:
            # 被占用/权限不足都跳过，不能因为一个文件删不掉就中断整轮清理
            continue
        total -= size
        removed.append(path)

    if removed:
        _drop_empty_day_dirs(base_dir)
    return removed

