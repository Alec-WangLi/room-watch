"""人脸检测：只在 YOLO 已经框出的「人」里面找脸。

为什么不全图找脸？
1. 快。只在人形框那一小块区域跑检测，开销可以忽略。
2. 准。在全图上找脸，很容易把墙上的开关、画框、衣物图案误判成脸；
   而 YOLO 已经告诉你「这里有个人」，在这块区域里找脸，误报率低得多。

检测器用 OpenCV 的 **YuNet**（DNN，比 Haar 级联准得多，模型仅 230KB，
放在 models/ 目录里，不联网也能跑）。
如果 YuNet 模型缺失，会退回到 Haar 级联；两者都没有则自动禁用，
不会让主程序崩掉。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .paths import resolve
from .rules import Detection

log = logging.getLogger("room-watch")

YUNET_NAME = "face_detection_yunet_2023mar.onnx"
HAAR_NAME = "haarcascade_frontalface_default.xml"
DEFAULT_MODEL_DIR = "models"


@dataclass(frozen=True)
class FaceHit:
    """一张脸：框 + 原始关键点行。

    `row` 是 YuNet 原始的 15 维数组（x, y, w, h, 5 组关键点, 置信度），
    并且已经换算成**原图坐标**。人脸识别（identity.py）的 alignCrop 必须吃这个，
    因为对齐要按关键点把脸摆正——只给框是摆不正的。
    Haar 回退没有关键点，此时 row 为 None，识别会自动跳过（判为 unknown）。
    """

    box: Detection
    row: Optional[np.ndarray] = None


class FaceFinder:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.score_threshold = float(cfg.get("score_threshold", 0.7))
        self.nms_threshold = float(cfg.get("nms_threshold", 0.3))
        self.padding = int(cfg.get("padding", 12))
        self.min_crop = int(cfg.get("min_crop", 40))

        self.backend = None
        self._yunet = None
        self._haar = None

        if self.enabled:
            self._init_detector(cfg)

    def _init_detector(self, cfg: dict) -> None:
        model_path = cfg.get("model")
        if not model_path:
            model_path = os.path.join(DEFAULT_MODEL_DIR, YUNET_NAME)
        # 相对路径按项目根解析，而不是按本文件所在目录 ——
        # 包化后 __file__ 在 roomwatch/ 里，models/ 却在项目根。
        model_path = resolve(model_path)

        if os.path.exists(model_path):
            try:
                # 输入尺寸每次由 setInputSize 指定，这里先给个占位值
                self._yunet = cv2.FaceDetectorYN.create(
                    model_path, "", (320, 320), self.score_threshold, self.nms_threshold, 50
                )
                self.backend = "yunet"
                log.info("人脸检测器: YuNet (%s)", os.path.basename(model_path))
                return
            except Exception as exc:
                log.warning("YuNet 初始化失败，尝试 Haar: %s", exc)

        haar_path = os.path.join(cv2.data.haarcascades, HAAR_NAME)
        if os.path.exists(haar_path):
            self._haar = cv2.CascadeClassifier(haar_path)
            if not self._haar.empty():
                self.backend = "haar"
                log.info("人脸检测器: Haar 级联")
                return

        log.error(
            "找不到可用的剪脸检测器（YuNet 模型缺失，且 OpenCV 5 已不再自带 Haar 级联）。"
            "语音提示将退化为「检测到人就出声」。"
        )
        self.enabled = False

    def find_full(self, frame, person_dets: list) -> list:
        """在人形框里找脸，返回 FaceHit 列表（含原图坐标的关键点行）。"""
        if not self.enabled or not person_dets:
            return []

        h, w = frame.shape[:2]
        found = []

        for d in person_dets:
            x1 = max(0, int(d.x1) - self.padding)
            y1 = max(0, int(d.y1) - self.padding)
            x2 = min(w, int(d.x2) + self.padding)
            y2 = min(h, int(d.y2) + self.padding)
            if x2 - x1 < self.min_crop or y2 - y1 < self.min_crop:
                continue

            crop = frame[y1:y2, x1:x2]
            if self.backend == "yunet":
                faces = self._detect_yunet(crop)
            else:
                faces = self._detect_haar(crop)

            for item in faces:
                fx, fy, fw, fh = item[:4]
                row = item[4] if len(item) > 4 else None
                if row is not None:
                    # 关键点原本是裁剪图坐标，必须补回偏移才是原图坐标。
                    # 不补的话 alignCrop 会把脸摆歪，特征全废（而且不报错）。
                    row = self._shift_row(row, x1, y1)
                found.append(
                    FaceHit(
                        box=Detection(
                            x1=float(x1 + fx),
                            y1=float(y1 + fy),
                            x2=float(x1 + fx + fw),
                            y2=float(y1 + fy + fh),
                            conf=1.0,
                        ),
                        row=row,
                    )
                )
        return found

    def find(self, frame, person_dets: list) -> list:
        """兼容旧接口：只要框，不要关键点。"""
        return [hit.box for hit in self.find_full(frame, person_dets)]

    @staticmethod
    def _shift_row(row, dx: int, dy: int) -> np.ndarray:
        """把裁剪图坐标的人脸行换算到原图坐标。

        YuNet 每行 15 个值：x, y, w, h, 5 组关键点(x,y), 置信度。
        平移只影响 x/y 与关键点，宽高和置信度不动。
        """
        r = np.array(row, dtype=np.float32).copy()
        r[0] += dx
        r[1] += dy
        for i in range(4, 14, 2):
            r[i] += dx
            r[i + 1] += dy
        return r


    def _detect_yunet(self, crop) -> list:
        try:
            ch, cw = crop.shape[:2]
            self._yunet.setInputSize((cw, ch))
            _retval, faces = self._yunet.detect(crop)
        except Exception as exc:
            log.debug("YuNet 检测异常: %s", exc)
            return []
        if faces is None:
            return []
        # 每行 15 个值：x, y, w, h, 5 组关键点, 置信度
        out = []
        for f in faces:
            x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
            if score >= self.score_threshold and fw > 0 and fh > 0:
                # 连原始 15 维行一起带出去：人脸识别要按关键点把脸摆正，
                # 只给框是摆不正的（而且 alignCrop 不会报错，只会给你一张歪脸）。
                out.append((x, y, fw, fh, np.asarray(f, dtype=np.float32).ravel()))
        return out

    def _detect_haar(self, crop) -> list:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        faces = self._haar.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        return [tuple(int(v) for v in f) for f in faces]
