"""人体检测器：ONNX（cv2.dnn，默认）或 ultralytics（torch，兜底）。

为什么默认 ONNX
---------------
torch 只在原来的 `from ultralytics import YOLO` 这一行被用到，却带进来 539MB 依赖
和「默认吃满所有核」的推理配置。同一张 720p 抓拍换成 ONNX 后实测：

    torch    19.0 ms  但 6 个核一起跑
    cv2.dnn  58.7 ms  2 线程，约 0.6 个核
    检测结果完全一致：框 (400,5)-(1263,706)，conf 0.93

单次慢 3 倍，但 CPU 总量只有 1/6；配合 gate 的空闲跳帧，整机占用降一个量级。
而且 **cv2.dnn 已经随 opencv 装好，不需要额外引入 onnxruntime**——少一个 50MB 依赖。

导出（构建分发包时做一次，运行时不需要 torch）：

    python -m roomwatch.onnx_detector --export yolov8n.pt --out models/yolov8n.onnx
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Optional

import cv2
import numpy as np

from .rules import Detection

log = logging.getLogger("room-watch")

DEFAULT_ONNX = "models/yolov8n.onnx"
PERSON_CLASS = 0


def letterbox(
    frame: np.ndarray, size: int = 640, color: int = 114
) -> tuple[np.ndarray, float, int, int]:
    """等比缩放 + 居中补边。返回 (画布, 缩放比, 左上角偏移 x, 偏移 y)。

    不能直接 resize 到 640x640：宽高比一变形，人的长宽比跟着变，检测框会漂。
    """
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError("空画面无法缩放")
    scale = min(size / float(w), size / float(h))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), color, dtype=np.uint8)
    dx, dy = (size - nw) // 2, (size - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, scale, dx, dy


def _as_candidates(pred: np.ndarray) -> np.ndarray:
    """统一成 (候选数, 4 + 类别数)。

    YOLOv8 的 ONNX 输出通常直接是 (1, 84, 8400)，但不同导出参数下也可能是
    (1, 8400, 84)。不能只用「谁大谁小」判断：候选数很少时（测试或极小图）
    会判反。先认「通道维 = 4 + 类别数」这个特征，再退回尺寸启发。
    """
    pred = np.asarray(pred, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.ndim != 2:
        raise ValueError(f"不认识的模型输出形状: {pred.shape}")

    channel_like = (84, 85, 116)  # COCO 80 类 / 带 objectness / 自定义常见值
    if pred.shape[0] in channel_like and pred.shape[1] not in channel_like:
        return pred.T
    if pred.shape[1] in channel_like:
        return pred
    if pred.shape[0] < pred.shape[1] and pred.shape[1] > 128:
        return pred.T
    return pred


def decode(
    pred: np.ndarray,
    conf_thres: float,
    scale: float,
    pad: tuple[int, int],
    frame_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """把 YOLOv8 输出解成原图坐标系下的框。

    前 4 列是 cx,cy,w,h，之后是类别分数；只取 person 那一列（索引 4）。
    """
    pred = _as_candidates(pred)

    if pred.shape[1] < 5:
        return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)

    scores = pred[:, 4]
    keep = scores >= conf_thres
    if not np.any(keep):
        return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)

    picked = pred[keep]
    scores = scores[keep]
    cx, cy, bw, bh = picked[:, 0], picked[:, 1], picked[:, 2], picked[:, 3]
    dx, dy = pad
    x1 = (cx - bw / 2.0 - dx) / scale
    y1 = (cy - bh / 2.0 - dy) / scale
    x2 = (cx + bw / 2.0 - dx) / scale
    y2 = (cy + bh / 2.0 - dy) / scale

    fh, fw = frame_shape
    boxes = np.stack([x1, y1, x2, y2], axis=1)
    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, fw)
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, fh)

    widths = boxes[:, 2] - boxes[:, 0]
    heights = boxes[:, 3] - boxes[:, 1]
    valid = (widths >= 1) & (heights >= 1)
    return boxes[valid].astype(np.float32), scores[valid].astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float = 0.45) -> np.ndarray:
    """标准贪心 NMS。自己实现而不用 cv2.dnn.NMSBoxes，是为了能离线单测。"""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=int)

    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []

    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[iou <= iou_thres]

    return np.array(keep, dtype=int)


class OnnxDetector:
    """cv2.dnn 跑 YOLOv8 ONNX。只做 person 一类。"""

    name = "onnx"

    def __init__(
        self,
        model_path: str,
        imgsz: int = 640,
        conf: float = 0.55,
        iou: float = 0.45,
        threads: int = 2,
    ) -> None:
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"找不到 ONNX 模型 {model_path}。"
                "请先执行 python -m roomwatch.onnx_detector --export yolov8n.pt"
            )
        self.model_path = model_path
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.threads = max(1, int(threads))

        self.net = cv2.dnn.readNetFromONNX(model_path)
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        try:
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except cv2.error:
            # OpenCV 5 的新图引擎不支持显式指定 target，走默认 CPU 即可
            pass

    def detect(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, dx, dy = letterbox(frame, self.imgsz)
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 255.0, (self.imgsz, self.imgsz), swapRB=True, crop=False
        )
        self.net.setInput(blob)
        out = self.net.forward()
        h, w = frame.shape[:2]
        boxes, scores = decode(out, self.conf, scale, (dx, dy), (h, w))
        keep = nms(boxes, scores, self.iou)
        return [
            Detection(
                x1=float(boxes[i][0]),
                y1=float(boxes[i][1]),
                x2=float(boxes[i][2]),
                y2=float(boxes[i][3]),
                conf=float(scores[i]),
            )
            for i in keep
        ]


class OnnxRuntimeDetector:
    """ONNX Runtime 推理后端（可选，不默认捆绑）。

    只在基准证明有明显收益时才考虑切换；未安装 onnxruntime 时构造会明确报错，
    模块导入本身不受影响（惰性 import）。
    """

    name = "onnxruntime"

    def __init__(
        self,
        model_path: str,
        imgsz: int = 640,
        conf: float = 0.55,
        iou: float = 0.45,
        threads: int = 2,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("onnxruntime 未安装") from exc

        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"找不到 ONNX 模型 {model_path}。"
                "请先执行 python -m roomwatch.onnx_detector --export yolov8n.pt"
            )
        self.model_path = model_path
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.threads = max(1, int(threads))

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            model_path, sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, dx, dy = letterbox(frame, self.imgsz)
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 255.0, (self.imgsz, self.imgsz), swapRB=True, crop=False
        )
        out = self.session.run(None, {self.input_name: blob})[0]
        h, w = frame.shape[:2]
        boxes, scores = decode(out, self.conf, scale, (dx, dy), (h, w))
        keep = nms(boxes, scores, self.iou)
        return [
            Detection(
                x1=float(boxes[i][0]),
                y1=float(boxes[i][1]),
                x2=float(boxes[i][2]),
                y2=float(boxes[i][3]),
                conf=float(scores[i]),
            )
            for i in keep
        ]


class TorchDetector:
    """兜底实现：没有 ONNX 文件时仍能跑，但会把 torch 拖回来。"""

    name = "torch"

    def __init__(
        self, model_path: str, imgsz: int = 640, conf: float = 0.55, iou: float = 0.45,
        threads: int = 2,
    ) -> None:
        from ultralytics import YOLO

        self.model_path = model_path
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.model = YOLO(model_path)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        result = self.model.predict(
            frame, classes=[PERSON_CLASS], conf=self.conf, iou=self.iou,
            imgsz=self.imgsz, verbose=False,
        )[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        out = []
        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            out.append(Detection(x1, y1, x2, y2, float(box.conf[0])))
        return out


def build_detector(cfg: dict, base_dir: Optional[str] = None):
    """按配置造检测器。engine=auto 时优先 ONNX，文件不在才退回 torch。"""
    cfg = cfg or {}
    engine = str(cfg.get("engine", "auto")).strip().lower()
    imgsz = int(cfg.get("imgsz", 640))
    conf = float(cfg.get("conf", 0.55))
    iou = float(cfg.get("iou", 0.45))
    threads = int(cfg.get("threads", 2))

    def pick(path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(base_dir or "", path)

    onnx_path = pick(str(cfg.get("onnx_model") or DEFAULT_ONNX))
    pt_path = pick(str(cfg.get("model") or "yolov8n.pt"))

    if engine == "torch":
        return TorchDetector(pt_path, imgsz, conf, iou, threads)
    if engine == "onnxruntime":
        return OnnxRuntimeDetector(onnx_path, imgsz, conf, iou, threads)
    if engine == "onnx":
        return OnnxDetector(onnx_path, imgsz, conf, iou, threads)

    if os.path.isfile(onnx_path):
        return OnnxDetector(onnx_path, imgsz, conf, iou, threads)
    log.warning("没找到 %s，退回 torch（体积与 CPU 占用都会更高）", onnx_path)
    return TorchDetector(pt_path, imgsz, conf, iou, threads)


def export(pt_path: str = "yolov8n.pt", out: str = DEFAULT_ONNX, imgsz: int = 640) -> str:
    """把 .pt 导成静态 shape 的 ONNX。构建分发包时跑一次。"""
    from ultralytics import YOLO

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    model = YOLO(pt_path)
    produced = model.export(format="onnx", imgsz=imgsz, opset=12, simplify=False, dynamic=False)
    produced = str(produced)
    if os.path.abspath(produced) != os.path.abspath(out):
        os.replace(produced, out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="YOLOv8 导出 ONNX（构建期用，运行时不需要）")
    ap.add_argument("--export", metavar="PT", default="yolov8n.pt")
    ap.add_argument("--out", default=DEFAULT_ONNX)
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = export(args.export, args.out, args.imgsz)
    size_mb = os.path.getsize(path) / 1048576
    print(f"已导出 {path}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
