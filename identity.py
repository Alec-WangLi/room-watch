"""人脸识别：区分「你本人」和「其他人」。

为什么需要这个
--------------
人脸**检测**（faces.py 里的 YuNet）只回答「这里有没有脸」；
你坐回电脑前，检测器照样认为「有脸」，于是监控把你当访客抓拍、播报、推送。
实测就发生过：一晚上 7 张抓拍里 6 张是本人在桌前。

要区分「谁」，必须做人脸**识别**（1:1 比对），这是完全不同的模型。

技术选型：OpenCV 自带的 SFace
-----------------------------
`cv2.FaceRecognizerSF` + `face_recognition_sface_2021dec.onnx`（约 37MB）。
选它的理由：
- OpenCV 已装，不用再装 dlib/face_recognition（那两个在 Windows 上要编译，坑极多）
- 输入就是 YuNet 输出的 15 维人脸框（含 5 个关键点），两个模型天然配对
- 输出 128 维特征，余弦相似度比对，CPU 上单张几毫秒

一个必须知道的坑
----------------
`alignCrop()` 需要**原图**和**原图坐标系**的 15 维人脸行。
如果拿人形框里的裁剪图去对齐，关键点坐标是裁剪图内的，必须补回偏移量，
否则对齐出来的脸是错位的，特征全废——而它不会报错，只会让你认错人。

策略
----
识别结果分三种：
- owner   ：和注册特征足够像
- other   ：有脸，但不像你
- unknown ：没脸、背影、太远、没注册过

`unknown_policy` 决定 unknown 怎么处理：
- `alert`（默认）：当成「他人」照常告警。安全优先——宁可多报，不可漏报
- `ignore`：当成「本人」静默。适合你经常背对镜头、又只想抓陌生人

多帧投票
--------
单帧识别会抖（侧脸、逆光某一帧相似度掉下去）。所以连续 N 次判定 owner 才认，
中间只要断一次就重新计数。这个状态机在 `OwnerTracker` 里，可脱离模型单测。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

log = logging.getLogger("room-watch")

DEFAULT_MODEL = "models/face_recognition_sface_2021dec.onnx"
DEFAULT_OWNER_FILE = "identity/owner.npz"
FEATURE_DIM = 128

# 身份标签
LABEL_OWNER = "owner"
LABEL_OTHER = "other"
LABEL_UNKNOWN = "unknown"


def _abs(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个特征向量的余弦相似度。SFace 官方就是按这个判定的。"""
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def average_embedding(feats: Sequence[np.ndarray]) -> Optional[np.ndarray]:
    """把多张脸的特征平均成一个。

    先各自归一化再平均，避免某张特别亮/特别大的脸把整体拉偏。
    平均后再归一化一次，这样存下来的就是一个干净的「本人模板」。
    """
    usable = []
    for f in feats:
        v = np.asarray(f, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n > 1e-12:
            usable.append(v / n)
    if not usable:
        return None
    mean = np.mean(np.stack(usable), axis=0)
    n = float(np.linalg.norm(mean))
    if n <= 1e-12:
        return None
    return (mean / n).astype(np.float32)


def classify(score: float, threshold: float) -> str:
    """相似度 → 身份标签。没注册时调用方会先返回 unknown，不会走到这里。"""
    return LABEL_OWNER if score >= threshold else LABEL_OTHER


@dataclass
class OwnerTracker:
    """多帧投票：把抖动的单帧结论变成稳定的「本人在场」判断。

    为什么不做成「一次命中就算本人」：
    侧脸、逆光、遮挡的那一帧，相似度可能掉到 0.2，但那确实是你。
    反过来，保洁某个角度糊一下就冲到 0.5，也不能认。所以用连续性投票。

    confirm_frames 是「连续命中」的要求，中间断一次立刻清零——
    宁可晚 1 秒认出来，也不要连续误认。
    """

    confirm_frames: int = 2
    streak: int = 0
    owner_present: bool = False
    last_score: float = 0.0

    def reset(self) -> None:
        self.streak = 0
        self.owner_present = False
        self.last_score = 0.0

    def observe(self, label: str) -> bool:
        """喂一帧的身份标签，返回「当前是否认定本人在场」。

        认定之后不会因为一帧 other 就翻脸——人转个头、低一下头都可能丢脸，
        真正解除要调用 reset()（由「画面里没人」来触发）。
        """
        if label == LABEL_OWNER:
            self.streak += 1
            if self.streak >= self.confirm_frames:
                self.owner_present = True
            return self.owner_present

        self.streak = 0
        return self.owner_present


class FaceEmbedder:
    """SFace 的薄封装：对齐 → 提特征 → 比对。模型缺失时优雅降级。"""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.model_path = _abs(str(cfg.get("model") or DEFAULT_MODEL))
        self.enabled = False
        self.last_error: Optional[str] = None
        self._rec = None

        if not os.path.exists(self.model_path):
            self.last_error = f"模型不存在: {self.model_path}"
            log.error(
                "人脸识别模型缺失，识别功能关闭（会退化成「所有人都是访客」）。"
                "下载地址见 README 的「人脸识别」章节。"
            )
            return

        try:
            import cv2

            self._rec = cv2.FaceRecognizerSF.create(self.model_path, "")
            self.enabled = True
            log.info("人脸识别器: SFace (%s)", os.path.basename(self.model_path))
        except Exception as exc:  # noqa: BLE001 - OpenCV 抛的异常类型不稳定
            self.last_error = str(exc)
            log.error("SFace 初始化失败，识别功能关闭: %s", exc)

    def embed(self, image, face_row) -> Optional[np.ndarray]:
        """从一张图 + YuNet 的 15 维人脸行里提特征。

        face_row 必须是**原图坐标系**（见模块头注释里的坑）。
        """
        if not self.enabled or image is None or face_row is None:
            return None
        try:
            aligned = self._rec.alignCrop(image, face_row)
            if aligned is None or getattr(aligned, "size", 0) == 0:
                return None
            feat = self._rec.feature(aligned)
            if feat is None:
                return None
            v = np.asarray(feat, dtype=np.float32).ravel()
            n = float(np.linalg.norm(v))
            return (v / n).astype(np.float32) if n > 1e-12 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("提取人脸特征失败: %s", exc)
            return None

    def match(self, a: np.ndarray, b: np.ndarray) -> float:
        return cosine_similarity(a, b)


class Identity:
    """对外门面：加载「本人模板」，判断一张脸是谁。

    没注册过模板时，identify() 一律返回 unknown——
    这样在用户还没注册之前，系统行为等于「所有人都是访客」，不会静默漏报。
    """

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.threshold = float(cfg.get("threshold", 0.45))
        self.owner_file = _abs(str(cfg.get("owner_file") or DEFAULT_OWNER_FILE))
        self.owner: Optional[np.ndarray] = None
        self.owner_samples = 0
        self.embedder = FaceEmbedder(cfg) if self.enabled else FaceEmbedder({"model": "\0"})
        if not self.enabled:
            log.info("人脸识别在 config.yaml 里被关掉了（identity.enabled: false）")

        if self.enabled and self.embedder.enabled:
            self._load_owner()

    @property
    def usable(self) -> bool:
        """能不能真正区分身份：模型可用 + 已注册模板。"""
        return bool(self.enabled and self.embedder.enabled and self.owner is not None)

    def _load_owner(self) -> None:
        if not os.path.exists(self.owner_file):
            log.warning(
                "尚未注册本人人脸（%s 不存在），识别功能空转："
                "所有进入者都会按访客处理。注册命令: python identity.py --enroll",
                self.owner_file,
            )
            return
        try:
            data = np.load(self.owner_file, allow_pickle=False)
            self.owner = np.asarray(data["embedding"], dtype=np.float32).ravel()
            self.owner_samples = int(data["samples"]) if "samples" in data else 0
            log.info(
                "已加载本人人脸模板（%d 张样本，阈值 %.2f）", self.owner_samples, self.threshold
            )
        except Exception as exc:  # noqa: BLE001
            log.error("读取本人人脸模板失败，识别功能空转: %s", exc)
            self.owner = None

    def save_owner(self, feats: Sequence[np.ndarray], extra: Optional[dict] = None) -> bool:
        """把多张脸的特征平均后存盘。返回是否成功。"""
        template = average_embedding(feats)
        if template is None:
            log.error("注册失败：没有可用的有效特征（图片里没找到清晰正脸？）")
            return False
        os.makedirs(os.path.dirname(self.owner_file) or ".", exist_ok=True)
        payload = {
            "embedding": template,
            "samples": np.array(len(feats), dtype=np.int32),
        }
        for k, v in (extra or {}).items():
            payload[k] = np.array(v)
        try:
            np.savez(self.owner_file, **payload)
        except Exception as exc:  # noqa: BLE001
            log.error("保存本人模板失败: %s", exc)
            return False
        self.owner = template
        self.owner_samples = len(feats)
        log.info("已保存本人人脸模板：%s（%d 张样本）", self.owner_file, len(feats))
        return True

    def identify(self, image, face_row) -> tuple[str, float]:
        """判断这张脸是谁，返回 (label, score)。

        没有注册模板时返回 ("unknown", 0.0)——调用方按 unknown_policy 处理。
        """
        if not self.usable:
            return LABEL_UNKNOWN, 0.0
        feat = self.embedder.embed(image, face_row)
        if feat is None:
            return LABEL_UNKNOWN, 0.0
        score = cosine_similarity(self.owner, feat)
        return classify(score, self.threshold), score

    def decide(self, labels: Sequence[tuple[str, float]]) -> tuple[str, float]:
        """一帧里可能有多张脸，合并成一个结论。

        规则（安全优先）：
        - 只要有一张不像你 → 认定「有他人」（哪怕你自己也在画面里）
        - 全是 owner → 本人
        - 全是 unknown → unknown（交给 unknown_policy 决定）
        - 没有人脸 → 没有结论，返回 unknown
        """
        if not labels:
            return LABEL_UNKNOWN, 0.0

        others = [s for lab, s in labels if lab == LABEL_OTHER]
        if others:
            # 取「最不像你」的那张作为代表分数，让日志更能反映风险
            return LABEL_OTHER, min(others)

        owners = [s for lab, s in labels if lab == LABEL_OWNER]
        if owners:
            return LABEL_OWNER, max(owners)

        return LABEL_UNKNOWN, 0.0
