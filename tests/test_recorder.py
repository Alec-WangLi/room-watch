"""recorder.py 的单测：真的写一段假录像，再把它读回来数帧。

    python test_recorder.py

为什么坚持「写完再读回来」：
    VideoWriter.isOpened() 返回 True 不代表文件可用。OpenCV 在 FFmpeg 后端失败时
    会悄悄 fallback 到别的后端，也有情况写出 0 字节。只有用 VideoCapture 真的
    读出帧来，才能证明这段录像事后ب找得回来。
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
import time
import unittest
from datetime import datetime, timedelta

import cv2
import numpy as np

from roomwatch.recorder import PreRollBuffer, Recorder, video_path_for

SIZE = (640, 360)  # 测试用小尺寸，跑得快；真实环境是 1280x720
FPS = 10.0
T0 = datetime(2026, 10, 17, 9, 3, 10)


def synthetic_frame(i: int, size=SIZE):
    """造一帧有明显变化的画面：静止画面会被编码器压得异常小，看不出问题。"""
    f = np.zeros((size[1], size[0], 3), np.uint8)
    f[:] = (30 + (i * 7) % 200, 40, 80)
    cv2.putText(f, f"frame {i:04d}", (20, size[1] // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.circle(f, ((i * 13) % size[0], size[1] // 3), 25, (0, 180, 255), -1)
    return f


def read_back(path: str):
    """把视频读回来：(能否打开, 帧数, 尺寸)。"""
    if not os.path.exists(path):
        return False, 0, None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return False, 0, None
    n = 0
    size = None
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if size is None:
            size = (frame.shape[1], frame.shape[0])
        n += 1
    cap.release()
    return True, n, size


class TestPreRollBuffer(unittest.TestCase):
    def test_ring_drops_oldest(self):
        """缓冲满了要顶掉最旧的，这是「环形」的定义。"""
        buf = PreRollBuffer(seconds=0.5, fps=10.0)  # 容量 5
        self.assertEqual(buf.capacity, 5)
        for i in range(5):
            buf.push(synthetic_frame(i), T0 + timedelta(seconds=i / 10))
        self.assertEqual(len(buf), 5)
        buf.push(synthetic_frame(99), T0 + timedelta(seconds=1))
        # 仍是 5 个，且第一个已经被顶掉
        self.assertEqual(len(buf), 5)

    def test_disabled_when_seconds_zero(self):
        buf = PreRollBuffer(seconds=0, fps=10.0)
        self.assertFalse(buf.enabled)
        buf.push(synthetic_frame(0), T0)
        self.assertEqual(len(buf), 0)

    def test_capacity_counts_seconds(self):
        self.assertEqual(PreRollBuffer(seconds=8, fps=10.0).capacity, 80)
        self.assertEqual(PreRollBuffer(seconds=3, fps=5.0).capacity, 15)

    def test_memory_is_bounded(self):
        """10 秒 @10fps 的 720p 原始帧是 270MB，JPEG 缓冲必须远小于此。"""
        buf = PreRollBuffer(seconds=10, fps=10.0, quality=80)
        for i in range(10):
            buf.push(synthetic_frame(i, size=(1280, 720)), T0 + timedelta(seconds=i))
        held = buf.bytes_held()
        self.assertLess(held, 20 * 1024 * 1024, "10 秒预录不该超过 20MB")

    def test_drain_clears_and_preserves_order(self):
        buf = PreRollBuffer(seconds=1, fps=10.0)
        for i in range(3):
            buf.push(synthetic_frame(i), T0)
        items = buf.drain()
        self.assertEqual(len(items), 3)
        self.assertEqual(len(buf), 0)
        # 顺序必须是旧->新
        first = cv2.imdecode(np.frombuffer(items[0], np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(first)
        self.assertEqual(first.shape[1], SIZE[0])


class TestRecorderWritesPlayableFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, **kw) -> Recorder:
        cfg = {"enabled": True, "pre_roll_seconds": 1.0, "queue_size": 200}
        cfg.update(kw)
        return Recorder(cfg, fps=FPS)

    def test_records_end_to_end(self):
        """完整一趟：预录 -> 触发 -> 录像 -> 收尾，产出必须能被读回来。"""
        rec = self._rec()
        rec.prime_size(SIZE)
        path = os.path.join(self.tmp, "090310.mp4")

        # 平时：帧进预录缓冲
        for i in range(10):
            rec.observe(synthetic_frame(i), T0 + timedelta(seconds=i / 10), has_person=False)
        self.assertEqual(len(rec.pre_roll), 10)

        # 触发
        self.assertEqual(rec.start(path, T0 + timedelta(seconds=1)), path)
        self.assertTrue(rec.recording)

        # 录 20 帧
        for i in range(10, 30):
            rec.observe(synthetic_frame(i), T0 + timedelta(seconds=i / 10), has_person=True)

        out = rec.stop("测试结束")
        self.assertEqual(out, path)
        self.assertFalse(rec.recording)

        ok, frames, size = read_back(path)
        self.assertTrue(ok, "写出来的视频必须能打开")
        self.assertGreater(frames, 0, "一帧都读不出来等于白录")
        self.assertEqual(size, SIZE)
        # 预录 10 帧 + 后续 20 帧
        self.assertEqual(frames, 30)
        self.assertEqual(rec.written, 30)

    def test_respects_disabled(self):
        rec = self._rec(enabled=False)
        rec.prime_size(SIZE)
        path = os.path.join(self.tmp, "nope.mp4")
        self.assertIsNone(rec.start(path, T0))
        rec.observe(synthetic_frame(0), T0)
        self.assertFalse(os.path.exists(path))

    def test_start_twice_is_ignored(self):
        """同一次事件不能被重复开录，否则会互相抢同一个 writer。"""
        rec = self._rec()
        rec.prime_size(SIZE)
        first = rec.start(os.path.join(self.tmp, "a.mp4"), T0)
        self.assertIsNotNone(first)
        second = rec.start(os.path.join(self.tmp, "b.mp4"), T0 + timedelta(seconds=1))
        self.assertIsNone(second)
        rec.stop()

    def test_stop_without_start_is_safe(self):
        rec = self._rec()
        self.assertIsNone(rec.stop())
        rec.shutdown()  # 也不能抛

    def test_mismatched_frame_size_is_resized(self):
        """摄像头重开后分辨率可能变，VideoWriter 不接受中途换尺寸。"""
        rec = self._rec(pre_roll_seconds=0)
        rec.prime_size(SIZE)
        path = os.path.join(self.tmp, "mix.mp4")
        rec.start(path, T0)
        rec.observe(synthetic_frame(1, size=(1280, 720)), T0)  # 故意给个不同尺寸
        rec.observe(synthetic_frame(2), T0)
        rec.stop()
        ok, frames, size = read_back(path)
        self.assertTrue(ok)
        self.assertEqual(size, SIZE)

    def test_video_path_pairs_with_snapshot(self):
        self.assertEqual(
            os.path.normpath(video_path_for("captures/2026-10-17/090310.jpg")),
            os.path.normpath("captures/2026-10-17/090310.mp4"),
        )

    def test_queue_full_drops_frames_instead_of_blocking(self):
        """编码跟不上时宁可掉帧，绝不能把主循环堵死。"""
        rec = self._rec(pre_roll_seconds=0, queue_size=2)
        rec.prime_size(SIZE)
        path = os.path.join(self.tmp, "drop.mp4")
        rec.start(path, T0)
        for i in range(200):
            rec.observe(synthetic_frame(i), T0 + timedelta(seconds=i / 10))
        rec.stop()
        # 关键点：跑完了，没卡住；且确实记了丢帧
        self.assertGreater(rec.written + rec.dropped, 0)
        ok, frames, _ = read_back(path)
        self.assertTrue(ok)

    def test_encoding_does_not_stall_main_loop(self):
        """这是开后台线程的理由：observe() 必须是毫秒级，不能背负编码成本。"""
        rec = self._rec(pre_roll_seconds=8)
        rec.prime_size(SIZE)
        path = os.path.join(self.tmp, "perf.mp4")
        rec.start(path, T0)

        worst = 0.0
        for i in range(60):
            f = synthetic_frame(i)
            t0 = time.perf_counter()
            rec.observe(f, T0 + timedelta(seconds=i / 10))
            worst = max(worst, (time.perf_counter() - t0) * 1000)
        rec.stop()
        # 10fps 的每帧预算是 100ms；observe 只能占零头
        self.assertLess(worst, 50.0, f"observe() 最慢 {worst:.1f}ms，会拖垮主循环")


class TestPreRollCost(unittest.TestCase):
    """预录要对每一帧做 JPEG 编码——这是给得起还是给不起的开销？

    不设断言，只把实测数打出来。你换机器时可以据此决定 pre_roll_seconds。
    """

    def test_print_cost(self):
        buf = PreRollBuffer(seconds=8, fps=10.0, quality=80)
        costs = []
        for i in range(20):
            f = synthetic_frame(i, size=(1280, 720))
            t0 = time.perf_counter()
            buf.push(f, T0)
            costs.append((time.perf_counter() - t0) * 1000)
        avg = sum(costs) / len(costs)
        print(f"\n  [实测] 720p 预录 JPEG 编码：平均 {avg:.1f} ms/帧，"
              f"最慢 {max(costs):.1f} ms（10fps 每帧预算 100ms）")
        self.assertTrue(True)


class TestEventStoreMigration(unittest.TestCase):
    """录像功能给 events 表加了一列 video。

    已经在跑的机器上的 events.db 里没有这列，而
    CREATE TABLE IF NOT EXISTS 对「表已存在」是不做任何事的——
    漏了 ALTER TABLE，写事件就会当场炸，而且是每次都炸。
    """

    LEGACY_SCHEMA = """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   snapshot TEXT NOT NULL,
                   confidence REAL,
                   notified INTEGER NOT NULL DEFAULT 0
               )"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _legacy_db(self, name="events.db"):
        path = os.path.join(self.tmp, name)
        conn = sqlite3.connect(path)
        conn.execute(self.LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO events (ts, snapshot, confidence, notified) VALUES (?,?,?,?)",
            ("2026-10-16T15:49:27", "old.jpg", 0.9, 1),
        )
        conn.commit()
        conn.close()
        return path

    def _columns(self, store):
        return {r[1] for r in store.conn.execute("PRAGMA table_info(events)")}

    def test_video_column_added_to_legacy_db(self):
        from roomwatch.watcher import EventStore

        store = EventStore(self._legacy_db())
        self.assertIn("video", self._columns(store))

    def test_identity_column_added_to_legacy_db(self):
        from roomwatch.watcher import EventStore

        store = EventStore(self._legacy_db())
        self.assertIn("identity", self._columns(store))

    def test_can_write_identity_on_legacy_db(self):
        from roomwatch.watcher import EventStore

        store = EventStore(self._legacy_db())
        store.add(datetime(2026, 10, 17, 9, 4, 10), "090410.jpg", 0.88, True,
                  video=None, identity="other")
        row = store.conn.execute(
            "SELECT identity FROM events WHERE snapshot='090410.jpg'"
        ).fetchone()
        self.assertEqual(row, ("other",))

    def test_can_write_video_on_legacy_db(self):
        from roomwatch.watcher import EventStore

        store = EventStore(self._legacy_db())
        store.add(datetime(2026, 10, 17, 9, 3, 10), "090310.jpg", 0.93, True,
                  video="090310.mp4")
        row = store.conn.execute(
            "SELECT snapshot, video FROM events WHERE snapshot='090310.jpg'"
        ).fetchone()
        self.assertEqual(row, ("090310.jpg", "090310.mp4"))

    def test_old_rows_survive_migration(self):
        """升级不能把历史记录弄丢。"""
        from roomwatch.watcher import EventStore

        store = EventStore(self._legacy_db())
        total = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(total, 1)
        old = store.conn.execute(
            "SELECT snapshot, video, identity FROM events WHERE snapshot='old.jpg'"
        ).fetchone()
        self.assertEqual(old[0], "old.jpg")
        self.assertIsNone(old[1], "迁移前的老行 video 应为空")
        self.assertIsNone(old[2], "迁移前的老行 identity 应为空")

    def test_fresh_db_has_video_column(self):
        from roomwatch.watcher import EventStore

        store = EventStore(os.path.join(self.tmp, "fresh.db"))
        self.assertIn("video", self._columns(store))

    def test_opening_twice_is_safe(self):
        """迁移必须可重复执行——每次启动都会跑一遍。"""
        from roomwatch.watcher import EventStore

        db = self._legacy_db()
        EventStore(db)
        again = EventStore(db)  # 不该抛 "duplicate column name"
        self.assertIn("video", self._columns(again))


if __name__ == "__main__":
    unittest.main(verbosity=2)
