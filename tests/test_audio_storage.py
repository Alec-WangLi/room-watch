"""音轨合并与容量清理的纯逻辑测试。

这两块都属于「算错了不会报错、只会悄悄出问题」的类型：
- 音轨延迟算错 → 声音和画面对不上，但文件看起来完全正常
- 容量清理算错 → 要么永远清不掉（磁盘写满），要么把该留的删了（证据没了）
所以每个边界都钉住。
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import shutil
import tempfile
import time
import unittest
import wave
from datetime import datetime, timedelta

import numpy as np

from roomwatch import audio_recorder as A
from roomwatch import rules as L


class TestMergeCommand(unittest.TestCase):
    def test_no_delay_means_no_filter(self):
        """没有预录就不要加 -af，否则平白多一次滤镜链开销。"""
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "o.mp4", 0)
        self.assertNotIn("-af", cmd)

    def test_delay_is_applied_to_all_channels(self):
        """只写一个数字在多声道时会漏掉右声道，必须用 all=1。"""
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "o.mp4", 8000)
        idx = cmd.index("-af")
        self.assertEqual(cmd[idx + 1], "adelay=8000:all=1")

    def test_video_stream_is_copied_not_reencoded(self):
        """视频必须 copy：重编码既慢又掉画质，而这里只想加音轨。"""
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "o.mp4", 0)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")

    def test_shortest_prevents_trailing_silence(self):
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "o.mp4", 0)
        self.assertIn("-shortest", cmd)

    def test_output_is_last_arg(self):
        """ffmpeg 把最后一个参数当输出文件，顺序错了会覆盖源文件。"""
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "out.mp4", 500)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_y_overwrite_present(self):
        """不加 -y 时 ffmpeg 会停下来等输入，长期无人值守会永久卡住。"""
        cmd = A.build_merge_cmd("ffmpeg", "v.mp4", "a.wav", "o.mp4", 0)
        self.assertIn("-y", cmd)


class TestPreRollDelay(unittest.TestCase):
    def test_no_frames_no_delay(self):
        self.assertEqual(A.pre_roll_delay_ms(0, 10.0), 0)

    def test_eight_seconds_at_ten_fps(self):
        self.assertEqual(A.pre_roll_delay_ms(80, 10.0), 8000)

    def test_partial_buffer(self):
        """摄像头刚启动时缓冲没攒满，实际帧数少于配置，延迟要按实际算。"""
        self.assertEqual(A.pre_roll_delay_ms(30, 10.0), 3000)

    def test_zero_fps_does_not_crash(self):
        self.assertEqual(A.pre_roll_delay_ms(80, 0.0), 0)

    def test_negative_frames_is_zero(self):
        self.assertEqual(A.pre_roll_delay_ms(-5, 10.0), 0)


class TestWavDuration(unittest.TestCase):
    def test_reads_real_duration(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "t.wav")
            with wave.open(p, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(np.zeros(16000 * 2, dtype=np.int16).tobytes())  # 2 秒
            self.assertAlmostEqual(A.wav_duration(p), 2.0, places=2)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMediaEnumeration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, day: str, name: str, size: int, age_days: float = 0) -> str:
        folder = os.path.join(self.dir, day)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        if age_days:
            ts = time.time() - age_days * 86400
            os.utime(path, (ts, ts))
        return path

    def test_only_media_extensions_counted(self):
        self._make("2026-10-17", "a.jpg", 100)
        self._make("2026-10-17", "b.mp4", 200)
        self._make("2026-10-17", "notes.txt", 999)
        self.assertEqual(L.usage_bytes(self.dir), 300)

    def test_empty_dir_is_zero(self):
        self.assertEqual(L.usage_bytes(self.dir), 0)

    def test_missing_dir_is_zero_not_crash(self):
        self.assertEqual(L.usage_bytes(os.path.join(self.dir, "nope")), 0)


class TestPruneBySize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, day: str, name: str, size: int, age_days: float) -> str:
        folder = os.path.join(self.dir, day)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        ts = time.time() - age_days * 86400
        os.utime(path, (ts, ts))
        return path

    def test_under_limit_deletes_nothing(self):
        self._make("2026-10-17", "a.jpg", 100, 5)
        removed = L.prune_by_size(self.dir, 1000, datetime.now(), protect_seconds=0)
        self.assertEqual(removed, [])

    def test_over_limit_deletes_oldest_first(self):
        old = self._make("2026-10-10", "old.jpg", 400, 7)
        mid = self._make("2026-10-15", "mid.jpg", 400, 2)
        new = self._make("2026-10-17", "new.jpg", 400, 0.5)
        # 上限 800，总量 1200，需删掉 400 → 只该删最旧的
        removed = L.prune_by_size(self.dir, 800, datetime.now(), protect_seconds=0)
        self.assertEqual(len(removed), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(mid))
        self.assertTrue(os.path.exists(new))

    def test_deletes_multiple_until_under_limit(self):
        for i, age in enumerate([9, 8, 7]):
            self._make(f"2026-10-{10 + i}", f"f{i}.jpg", 500, age)
        # 总量 1500，上限 600 → 至少删掉 2 个
        removed = L.prune_by_size(self.dir, 600, datetime.now(), protect_seconds=0)
        self.assertGreaterEqual(len(removed), 2)
        self.assertLessEqual(L.usage_bytes(self.dir), 600)

    def test_protects_recently_written_files(self):
        """刚写出来的文件可能是正在录的那段，绝不能删。"""
        self._make("2026-10-17", "just_now.mp4", 500, 0)
        self._make("2026-10-10", "old.jpg", 500, 9)
        # 上限压到 100，但新文件在保护期内 → 只能删旧的那个
        removed = L.prune_by_size(self.dir, 100, datetime.now(), protect_seconds=300)
        self.assertEqual(len(removed), 1)
        self.assertIn("old.jpg", removed[0])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-10-17", "just_now.mp4")))

    def test_all_files_protected_deletes_nothing(self):
        """全是新文件就别硬删——宁可这轮没清够，也不能毁证据。"""
        self._make("2026-10-17", "a.mp4", 500, 0)
        removed = L.prune_by_size(self.dir, 10, datetime.now(), protect_seconds=300)
        self.assertEqual(removed, [])

    def test_empty_day_dir_is_removed(self):
        self._make("2026-10-01", "old.jpg", 900, 15)
        L.prune_by_size(self.dir, 100, datetime.now(), protect_seconds=0)
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "2026-10-01")))

    def test_zero_limit_means_disabled(self):
        p = self._make("2026-10-01", "old.jpg", 900, 15)
        removed = L.prune_by_size(self.dir, 0, datetime.now(), protect_seconds=0)
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(p))

    def test_missing_dir_is_safe(self):
        removed = L.prune_by_size(os.path.join(self.dir, "nope"), 100, datetime.now())
        self.assertEqual(removed, [])

    def test_never_touches_non_media(self):
        """清理只删自己产出的媒体文件，别的文件一概不碰。"""
        keep = os.path.join(self.dir, "2026-10-01", "config.bak")
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        with open(keep, "wb") as f:
            f.write(b"\0" * 900)
        ts = time.time() - 30 * 86400
        os.utime(keep, (ts, ts))
        L.prune_by_size(self.dir, 10, datetime.now(), protect_seconds=0)
        self.assertTrue(os.path.exists(keep))


if __name__ == "__main__":
    unittest.main(verbosity=2)
