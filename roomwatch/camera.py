"""采集源：本机 USB 摄像头 / RTSP 网络摄像头 / 视频文件。

为什么单独抽一层
----------------
旧代码是 `cv2.VideoCapture(int(camera.index), dshow)`，等于写死「只能是本机第 N 个
摄像头」。客户现场基本都是海康/大华那类 IP 摄像头，`rtsp://...` 传进来会被 `int()`
抛 ValueError 直接退出——连画面都取不到，这比「检测不准」严重得多。

这层只管三件事：把地址解析成明确的类型、按类型选对后端、给出可用的超时与
重连退避。主循环仍然只面对一个 cap 对象，不需要知道背后是 USB 还是 RTSP。

    rw start                              # camera.source 为空 → 用 camera.index
    camera.source: "rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101"
    camera.source: "D:/samples/room.mp4"   # 离线回放，用来调参
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Union
from urllib.parse import urlsplit, urlunsplit

import cv2

# 后端名 → OpenCV 常量。只对「本机设备编号」有意义；
# 网络流和文件必须走 FFmpeg，用 dshow 打不开。
BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}

DEVICE = "device"
URL = "url"
FILE = "file"


@dataclass(frozen=True)
class CameraSpec:
    """解析后的采集源。value 是传给 VideoCapture 的实参。"""

    kind: str
    value: Union[int, str]
    label: str


def parse_source(raw, index: int = 0) -> CameraSpec:
    """把配置里的 camera.source（或旧的 camera.index）变成 CameraSpec。

    认三种写法：
      0 / "0" / 1 / null   → 本机第 N 个摄像头
      "rtsp://..." "http://..." → 网络流
      存在的文件路径        → 视频文件

    其它字符串一律报错，不做「猜一个」：把写错的路径悄悄当成摄像头 0，
    现场表现是「能出画面但不是那个摄像头」，比直接报错难查得多。
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return CameraSpec(DEVICE, int(index), f"本机摄像头 #{int(index)}")

    if isinstance(raw, bool):
        raise ValueError(f"camera.source 不能是布尔值：{raw!r}")
    if isinstance(raw, int):
        return CameraSpec(DEVICE, raw, f"本机摄像头 #{raw}")

    text = str(raw).strip()
    if text.isdigit():
        return CameraSpec(DEVICE, int(text), f"本机摄像头 #{int(text)}")
    if "://" in text:
        return CameraSpec(URL, text, f"网络流 {mask_url(text)}")
    if os.path.isfile(text):
        return CameraSpec(FILE, text, f"视频文件 {os.path.basename(text)}")

    raise ValueError(
        "camera.source 无法识别："
        f"{text!r}。可以是摄像头编号（0/1）、rtsp:// 地址，或已存在的视频文件路径。"
    )


def mask_url(url: str) -> str:
    """日志里不能出现摄像头密码。rtsp://user:pass@host → rtsp://user:***@host"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.password:
        return url
    user = parts.username or ""
    host = parts.hostname or ""
    netloc = f"{user}:***@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def api_for(spec: CameraSpec, backend: str = "dshow") -> int:
    """设备用 Windows 的 dshow/msmf；网络流和文件必须 FFmpeg。"""
    if spec.kind == DEVICE:
        return BACKENDS.get(str(backend).lower(), cv2.CAP_DSHOW)
    return getattr(cv2, "CAP_FFMPEG", cv2.CAP_ANY)


def open_capture(
    spec: CameraSpec,
    backend: str = "dshow",
    width: int = 1280,
    height: int = 720,
    fps: int = 10,
    open_timeout_ms: int = 5000,
    read_timeout_ms: int = 5000,
) -> cv2.VideoCapture:
    """按源类型打开采集器。失败时返回未打开的 cap，由调用方决定重试。"""
    api = api_for(spec, backend)

    # FFmpeg 后端读 RTSP 默认走 UDP，丢包会花屏、断流后卡死。
    # 强制 TCP 并用 stimeout 让 read() 有上限，否则摄像头一掉线主循环就永久阻塞。
    if spec.kind == URL:
        opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
        wanted = "rtsp_transport;tcp"
        if "rtsp_transport" not in opts:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"{opts}|{wanted}" if opts else wanted
            )

    params: list[int] = []
    if spec.kind in (URL, FILE) and hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(open_timeout_ms),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(read_timeout_ms),
        ]

    try:
        cap = cv2.VideoCapture(spec.value, api, params) if params else cv2.VideoCapture(
            spec.value, api
        )
    except (cv2.error, TypeError):
        cap = cv2.VideoCapture(spec.value, api)

    if not cap.isOpened():
        return cap

    # 设备可以协商分辨率/帧率；RTSP 由摄像头决定，设置通常无效，靠日志看实际值。
    if spec.kind == DEVICE:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
    if spec.kind == URL and hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        # 缓冲越小延迟越低；RTSP 卡顿通常就是缓冲堆了几十帧
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def backoff_seconds(attempt: int, base: float = 3.0, cap: float = 60.0) -> float:
    """连续第 N 次重连失败后等多久。指数退避，封顶 cap 秒。

    摄像头被拔掉时不能每 100ms 疯狂重开，那会把日志刷满、CPU 也吃满。
    """
    if attempt <= 0:
        return 0.0
    return min(float(cap), float(base) * (2 ** (attempt - 1)))
