"""端到端：用假摄像头 + 假 YOLO 跑完整的 Watcher.run() 主循环。

    python test_integration.py

前面的单测各管一段，但它们加起来回答不了一个问题：
「真跑起来，从人进门到人走，录像到底会不会完整地出现在 captures 里？」

所以这里把摄像头和模型换成脚本驱动的假件，让 watcher 以为自己在正常监控。
脚本播完就抛 KeyboardInterrupt，正好走它真实的退出流程（含录像收尾）。

刻意关掉的东西：
- voice.enabled=False  —— 别在测试时对着音箱说话
- ensure_system_volume=0 —— 别真的动你的系统音量
- heartbeat=0 / dry_run —— 别发推送、别联网
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime

import cv2
import numpy as np

from roomwatch import watcher
from roomwatch.rules import Detection

SIZE = (320, 180)
FPS = 10.0


class ScriptedCamera:
    """按脚本吐帧。播完抛 KeyboardInterrupt，让主循环自然退出。"""

    def __init__(self, plan: list, size=SIZE):
        self.plan = list(plan)
        self.size = size
        self.i = 0
        self.current = None
        self.read_calls = 0

    def read(self):
        if self.i >= len(self.plan):
            raise KeyboardInterrupt("测试脚本播完")
        self.current = self.plan[self.i]
        self.i += 1
        self.read_calls += 1
        frame = np.zeros((self.size[1], self.size[0], 3), np.uint8)
        frame[:] = (30 + (self.i * 5) % 200, 60, 70)
        cv2.putText(frame, f"f{self.i:03d}", (20, 100),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        return True, frame

    def isOpened(self):
        return True

    def set(self, *_a, **_kw):
        return True

    def get(self, prop):
        return self.size[0] if prop == cv2.CAP_PROP_FRAME_WIDTH else self.size[1]

    def release(self):
        pass


class FakeBoxes(list):
    """list 子类：既能 len()，元素又能假扮 ultralytics 的 Boxes。"""

    @staticmethod
    def person():
        class B:
            xyxy = [type("A", (), {"tolist": lambda self: [60.0, 20.0, 260.0, 170.0]})()]
            conf = [0.93]

        return B()

    @staticmethod
    def make(n: int):
        return FakeBoxes([FakeBoxes.person() for _ in range(n)])


class ScriptedModel:
    """检测器的替身：这一段脚本里有人就返回一个框。

    接口对齐 onnx_detector：主循环只认 `detect(frame)`，不关心引擎是 ONNX 还是 torch。
    """

    name = "scripted"

    def __init__(self, cam: ScriptedCamera):
        self.cam = cam

    def detect(self, frame, **_kw):
        if not self.cam.current:
            return []
        return [Detection(x1=60.0, y1=20.0, x2=260.0, y2=170.0, conf=0.93)]


def read_back(path: str):
    if not os.path.exists(path):
        return False, 0
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return False, 0
    n = 0
    while True:
        ok, _ = cap.read()
        if not ok:
            break
        n += 1
    cap.release()
    return True, n


class FullRunTest(unittest.TestCase):
    """一次完整的「人进来 -> 录像 -> 人走 -> 收尾」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.captures = os.path.join(self.tmp, "captures")
        os.makedirs(self.captures, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, **record_over):
        record = {
            "enabled": True,
            "pre_roll_seconds": 1.0,   # 1 秒预录
            "min_seconds": 1.0,
            "idle_stop_seconds": 1.5,
            "max_seconds": 10.0,
            "queue_size": 200,
            "draw_boxes": False,
        }
        record.update(record_over)
        return {
            "camera": {"index": 0, "backend": "dshow", "width": SIZE[0],
                       "height": SIZE[1], "fps": FPS},
            "detector": {"conf": 0.5, "trigger_frames": 3, "capture_window": 8,
                         "cooldown_seconds": 90, "model": "fake"},
            "snapshot": {"dir": self.captures, "keep_days": 30,
                         "jpeg_quality": 80, "draw_boxes": False},
            "behavior": {"skip_when_user_active": False},
            "face": {"enabled": False},
            "voice": {"enabled": False, "ensure_system_volume": 0},
            "heartbeat": {"interval_minutes": 0, "stale_after_seconds": 120},
            "storage": {"database": os.path.join(self.tmp, "events.db"),
                        "status": os.path.join(self.tmp, "status.json")},
            "notify": {"channel": "none"},
            "record": record,
        }

    def _run(self, plan, **record_over):
        cfg = self._config(**record_over)
        w = watcher.Watcher(cfg, dry_run=True)
        cam = ScriptedCamera(plan)

        w.open_camera = lambda: (setattr(w, "cap", cam), True)[1]
        w.load_model = lambda: setattr(w, "detector", ScriptedModel(cam))
        try:
            w.run()
        except KeyboardInterrupt:
            # 理论上 run() 内部会吃掉它；万一漏出来也不算失败
            pass
        return w, cfg

    def test_full_event_produces_playable_video(self):
        # 10 帧空房间（喂预录）-> 连续 35 帧有人 -> 人走后再录够 idle 时长
        plan = [False] * 10 + [True] * 35 + [False] * 40
        w, cfg = self._run(plan)

        day = datetime.now().strftime("%Y-%m-%d")
        day_dir = os.path.join(self.captures, day)
        self.assertTrue(os.path.isdir(day_dir), "事件当天应当有归档目录")

        jpgs = [f for f in os.listdir(day_dir) if f.endswith(".jpg")]
        mp4s = [f for f in os.listdir(day_dir) if f.endswith(".mp4")]
        self.assertEqual(len(jpgs), 1, "整段脚本只该抓到一张（冷却期内不重复出图）")
        self.assertEqual(len(mp4s), 1, "整段脚本只该有一段录像")

        ok, frames = read_back(os.path.join(day_dir, mp4s[0]))
        self.assertTrue(ok, "录像必须能被打开")
        # 预录约 10 帧 + 在场 35 帧 + 离开后还录了一小会儿
        self.assertGreater(frames, 40, f"只录到 {frames} 帧，太少")
        self.assertFalse(w.recorder.recording, "退出后不该还在录")
        self.assertEqual(w.recorder.dropped, 0, f"不该丢帧，丢了 {w.recorder.dropped} 帧")

    def test_video_is_recorded_into_database(self):
        plan = [False] * 10 + [True] * 35 + [False] * 40
        _w, cfg = self._run(plan)
        db = cfg["storage"]["database"]
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT snapshot, video FROM events").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, "应当记录一条事件")
        snapshot, video = rows[0]
        self.assertTrue(snapshot.endswith(".jpg"))
        self.assertIsNotNone(video, "事件必须关联上录像路径")
        self.assertTrue(video.endswith(".mp4"))

    def test_status_reports_recording_field(self):
        import json

        plan = [False] * 10 + [True] * 35 + [False] * 40
        _w, cfg = self._run(plan)
        with open(cfg["storage"]["status"], encoding="utf-8") as f:
            status = json.load(f)
        self.assertIn("recording", status, "状态里要能看出是否在录")
        self.assertFalse(status["recording"], "退出后应为未在录")
        self.assertEqual(status["events_today"], 1)

    def test_recording_disabled_writes_no_video(self):
        plan = [False] * 10 + [True] * 35 + [False] * 40
        _w, cfg = self._run(plan, enabled=False)
        day_dir = os.path.join(self.captures, datetime.now().strftime("%Y-%m-%d"))
        mp4s = [f for f in os.listdir(day_dir) if f.endswith(".mp4")]
        self.assertEqual(mp4s, [], "关掉录像后不该有 mp4")
        jpgs = [f for f in os.listdir(day_dir) if f.endswith(".jpg")]
        self.assertEqual(len(jpgs), 1, "抓拍不受影响")

    def test_no_person_no_video(self):
        """纯空房间不能留下任何录像——没人就等于没事件。"""
        plan = [False] * 50
        _w, _cfg = self._run(plan)
        base = self.captures
        produced = [
            f
            for _root, _d, files in os.walk(base)
            for f in files
            if f.endswith((".mp4", ".jpg"))
        ]
        self.assertEqual(produced, [], "空房间不该产生任何文件")

    def test_long_visit_rolls_over_into_segments(self):
        """人一直不走时，单段到点后应当续录新文件，而不是把后半场丢掉。"""
        # 单段 1 秒、总额度 3 秒：约能出 3 段，之后即便人还在也必须停
        plan = [False] * 5 + [True] * 120
        w, _cfg = self._run(
            plan,
            max_seconds=1.0,
            min_seconds=0.3,
            idle_stop_seconds=5.0,
            max_total_seconds=3.0,
        )
        day_dir = os.path.join(self.captures, datetime.now().strftime("%Y-%m-%d"))
        mp4s = sorted(f for f in os.listdir(day_dir) if f.endswith(".mp4"))
        self.assertGreater(len(mp4s), 1, "到点后应续录分段，而不是就此截断")
        self.assertLessEqual(len(mp4s), 4, "总额度生效后不该无限写下去")

        for name in mp4s:
            ok, frames = read_back(os.path.join(day_dir, name))
            self.assertTrue(ok, f"{name} 打不开")
            self.assertGreater(frames, 0, f"{name} 是空的")

        self.assertGreaterEqual(w.record_policy.total_recorded, 3.0)
        self.assertFalse(w.recorder.recording)


if __name__ == "__main__":
    unittest.main(verbosity=2)
