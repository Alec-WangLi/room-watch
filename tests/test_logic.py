"""logic.py 的单测：不需要摄像头，不需要网络，直接跑。

    python test_logic.py
"""

from __future__ import annotations

# 直接运行 tests/xxx.py 时把项目根加入 sys.path；`rw test` / `-m unittest` 不需要这一步。
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from roomwatch.rules import (
    RECORD_CONTINUE,
    RECORD_STOP_IDLE,
    RECORD_STOP_MAX,
    RecordPolicy,
    gap_seconds,
    ACTION_BEST,
    ACTION_FIRE,
    ACTION_NONE,
    ACTION_START,
    Detection,
    TriggerState,
    frame_score,
    humanize_duration,
    is_stale,
    prune_old,
    should_heartbeat,
    should_speak,
    snapshot_path,
)

FRAME = (1280, 720)
T0 = datetime(2026, 10, 16, 14, 0, 0)


def person(x1=500, y1=150, x2=800, y2=650, conf=0.9) -> Detection:
    return Detection(x1=x1, y1=y1, x2=x2, y2=y2, conf=conf)


class TestTrigger(unittest.TestCase):
    def test_single_frame_noise_does_not_alert(self):
        """单帧误检不该触发——这是防误报的第一道关。"""
        s = TriggerState(trigger_frames=3)
        self.assertEqual(s.observe([person()], T0, FRAME), ACTION_NONE)
        # 人来了一下又没了，重新计数
        self.assertEqual(
            s.observe([], T0 + timedelta(seconds=1), FRAME), ACTION_NONE
        )
        self.assertEqual(s.streak, 0)

    def test_consecutive_frames_confirm_entry(self):
        s = TriggerState(trigger_frames=3)
        self.assertEqual(s.observe([person()], T0, FRAME), ACTION_NONE)
        self.assertEqual(
            s.observe([person()], T0 + timedelta(seconds=1), FRAME), ACTION_NONE
        )
        self.assertEqual(
            s.observe([person()], T0 + timedelta(seconds=2), FRAME), ACTION_START
        )

    def test_low_confidence_is_ignored(self):
        s = TriggerState(trigger_frames=2, min_conf=0.6)
        weak = person(conf=0.3)
        self.assertEqual(s.observe([weak], T0, FRAME), ACTION_NONE)
        self.assertEqual(
            s.observe([weak], T0 + timedelta(seconds=1), FRAME), ACTION_NONE
        )

    def test_better_frame_replaces_candidate(self):
        s = TriggerState(trigger_frames=2, capture_window=8)
        s.observe([person()], T0, FRAME)
        s.observe([person(500, 150, 800, 650)], T0, FRAME)  # ACTION_START

        # 人走近了，框变大 -> 换一张更好的
        action = s.observe(
            [person(400, 100, 900, 700)], T0 + timedelta(seconds=1), FRAME
        )
        self.assertEqual(action, ACTION_BEST)
        self.assertGreater(s.best_score, 0)

    def test_person_leaving_fires_event(self):
        """人进来又走了：应该在离开时用攒下的最好那张出图。"""
        s = TriggerState(trigger_frames=2, capture_window=8)
        s.observe([person()], T0, FRAME)
        s.observe([person()], T0, FRAME)
        action = s.observe([], T0 + timedelta(seconds=2), FRAME)
        self.assertEqual(action, ACTION_FIRE)

    def test_capture_window_full_fires_event(self):
        """人一直待着不出门：采集窗口满了也要出图，不能无限等。"""
        s = TriggerState(trigger_frames=1, capture_window=3)
        now = T0
        actions = []
        for _ in range(5):
            actions.append(s.observe([person(500, 150, 800, 650)], now, FRAME))
            now += timedelta(seconds=1)
        self.assertIn(ACTION_FIRE, actions)
        self.assertEqual(actions.count(ACTION_FIRE), 1)

    def test_cooldown_suppresses_repeat_alerts(self):
        """同一个人在房间里待着，不能反复推送把手机炸掉。"""
        s = TriggerState(trigger_frames=1, capture_window=1, cooldown_seconds=90)
        now = T0
        s.observe([person()], now, FRAME)  # START
        now += timedelta(seconds=1)
        self.assertEqual(s.observe([person()], now, FRAME), ACTION_FIRE)

        # 冷却期内再来人，应该完全静默
        for _ in range(10):
            now += timedelta(seconds=1)
            self.assertEqual(s.observe([person()], now, FRAME), ACTION_NONE)

        # 冷却结束后恢复
        now += timedelta(seconds=100)
        self.assertEqual(s.observe([person()], now, FRAME), ACTION_START)

    def test_cooldown_allows_zero(self):
        s = TriggerState(trigger_frames=1, capture_window=1, cooldown_seconds=0)
        s.observe([person()], T0, FRAME)
        self.assertEqual(
            s.observe([person()], T0 + timedelta(seconds=1), FRAME), ACTION_FIRE
        )
        self.assertEqual(
            s.observe([person()], T0 + timedelta(seconds=2), FRAME), ACTION_START
        )


class TestFrameScore(unittest.TestCase):
    def test_centered_large_person_scores_higher(self):
        good = frame_score(person(400, 100, 900, 700), FRAME)
        small = frame_score(person(600, 300, 700, 500), FRAME)
        self.assertGreater(good, small)

    def test_edge_touching_person_is_penalized(self):
        """半个身子在画面外，不适合当存档照。"""
        centered = frame_score(person(500, 200, 800, 600), FRAME)
        edge = frame_score(person(0, 200, 300, 600), FRAME)
        self.assertGreater(centered, edge)

    def test_degenerate_frame_size(self):
        self.assertEqual(frame_score(person(), (0, 0)), 0.0)


class TestHeartbeat(unittest.TestCase):
    def test_first_heartbeat_fires_immediately(self):
        self.assertTrue(should_heartbeat(T0, None, 30))

    def test_not_before_interval(self):
        self.assertFalse(should_heartbeat(T0 + timedelta(minutes=29), T0, 30))

    def test_fires_at_interval(self):
        self.assertTrue(should_heartbeat(T0 + timedelta(minutes=30), T0, 30))

    def test_after_missed_windows(self):
        self.assertTrue(should_heartbeat(T0 + timedelta(minutes=95), T0, 30))

    def test_disabled_when_interval_zero(self):
        self.assertFalse(should_heartbeat(T0 + timedelta(hours=5), T0, 0))


class TestStaleness(unittest.TestCase):
    def test_no_frame_yet_is_stale(self):
        self.assertTrue(is_stale(None, T0, 60))

    def test_fresh_frame_is_not_stale(self):
        self.assertFalse(is_stale(T0, T0 + timedelta(seconds=5), 60))

    def test_old_frame_is_stale(self):
        self.assertTrue(is_stale(T0, T0 + timedelta(seconds=120), 60))


class TestHumanize(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(humanize_duration(30), "30s")

    def test_minutes(self):
        self.assertEqual(humanize_duration(125), "2m05s")

    def test_hours(self):
        self.assertEqual(humanize_duration(3725), "1h02m")

    def test_negative_clamps(self):
        self.assertEqual(humanize_duration(-5), "0s")


class TestGapSeconds(unittest.TestCase):
    def test_parses_iso_string(self):
        iso = (T0 - timedelta(seconds=30)).isoformat()
        g = gap_seconds(iso, T0)
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g, 30.0, places=1)

    def test_accepts_datetime(self):
        self.assertAlmostEqual(gap_seconds(T0 - timedelta(seconds=5), T0), 5.0, places=1)

    def test_none_input(self):
        self.assertIsNone(gap_seconds(None, T0))

    def test_garbage_input(self):
        self.assertIsNone(gap_seconds("not-a-timestamp", T0))


class TestShouldSpeak(unittest.TestCase):
    def test_speaks_when_face_found(self):
        self.assertTrue(should_speak(T0, None, 60, face_found=True, require_face=True))

    def test_silent_without_face_when_required(self):
        """只看到背影或半个人时不要对着空气说话。"""
        self.assertFalse(should_speak(T0, None, 60, face_found=False, require_face=True))

    def test_speaks_without_face_when_not_required(self):
        self.assertTrue(should_speak(T0, None, 60, face_found=False, require_face=False))

    def test_cooldown_blocks_repeat(self):
        self.assertFalse(should_speak(T0 + timedelta(seconds=30), T0, 60, True, True))

    def test_speaks_again_after_cooldown(self):
        self.assertTrue(should_speak(T0 + timedelta(seconds=61), T0, 60, True, True))

    def test_first_time_always_allowed(self):
        self.assertTrue(should_speak(T0, None, 60, True, True))


class TestStatusAfterHardKill(unittest.TestCase):
    """进程被强杀时，状态文件里 stale 永远是 false。

    如果读取方直接信这个字段，就会把「早就死了」报成「在线」。
    这两条测试确保读取方在读取那一刻重算。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "status.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, **kw):
        data = {
            "pid": 1,
            "uptime": "10m",
            "last_frame_at": kw.get("last_frame_at"),
            "stale": False,  # 强杀留下的谎言
            "camera_ok": True,
            "frames": 100,
            "events_today": 2,
            "dry_run": False,
        }
        data.update(kw.get("extra", {}))
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_killed_process_reports_stopped(self):
        from roomwatch import gallery

        self._write(last_frame_at=(datetime.now() - timedelta(minutes=10)).isoformat())
        out = gallery.read_status(self.path, stale_after=120)
        self.assertIn("已停止", out)
        self.assertNotIn("在线", out)

    def test_live_process_reports_online(self):
        from roomwatch import gallery

        self._write(last_frame_at=datetime.now().isoformat())
        out = gallery.read_status(self.path, stale_after=120)
        self.assertIn("在线", out)

    def test_missing_status_file(self):
        from roomwatch import gallery

        self.assertIn("监控未运行", gallery.read_status(os.path.join(self.tmp, "nope.json")))


class TestArchive(unittest.TestCase):
    def test_snapshot_path_layout(self):
        p = snapshot_path("captures", datetime(2026, 10, 16, 14, 23, 5))
        self.assertEqual(os.path.normpath(p), os.path.normpath("captures/2026-10-16/142305.jpg"))

    def test_prune_removes_expired_days(self):
        base = tempfile.mkdtemp()
        try:
            old_day = os.path.join(base, "2026-09-01")
            new_day = os.path.join(base, "2026-10-15")
            os.makedirs(old_day)
            os.makedirs(new_day)
            with open(os.path.join(old_day, "120000.jpg"), "w") as f:
                f.write("x")

            removed = prune_old(base, keep_days=30, now=datetime(2026, 10, 16))
            self.assertIn(old_day, removed)
            self.assertFalse(os.path.exists(old_day))
            self.assertTrue(os.path.isdir(new_day))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_prune_keeps_recent_and_handles_junk(self):
        base = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(base, "2026-10-15"))
            open(os.path.join(base, "not-a-date.txt"), "w").close()
            removed = prune_old(base, keep_days=30, now=datetime(2026, 10, 16))
            self.assertEqual(removed, [])
            self.assertTrue(os.path.isdir(os.path.join(base, "2026-10-15")))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_prune_disabled_when_keep_days_zero(self):
        base = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(base, "2020-01-01"))
            self.assertEqual(prune_old(base, 0, datetime(2026, 10, 16)), [])
            self.assertTrue(os.path.isdir(os.path.join(base, "2020-01-01")))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestGallery(unittest.TestCase):
    """gallery 用 str.replace 而不是 str.format，因为 CSS 里有花括号。"""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        day = os.path.join(self.base, "2026-10-16")
        os.makedirs(day)
        with open(os.path.join(day, "143012.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_render_keeps_css_braces(self):
        from roomwatch import gallery

        html = gallery.render(self.base)
        self.assertIn("font-family", html)
        self.assertNotIn("{count}", html)
        self.assertNotIn("{days}", html)

    def test_render_lists_snapshots(self):
        from roomwatch import gallery

        html = gallery.render(self.base)
        self.assertIn("2026-10-16/143012.jpg", html)
        self.assertIn("14:30:12", html)

    def test_render_empty_dir(self):
        from roomwatch import gallery

        self.assertIn("还没有抓拍", gallery.render("definitely-not-here"))


class TestRecordPolicy(unittest.TestCase):
    """录像停止策略：人还在别停，人走了别磨，但永远碰得到天花板。"""

    def _policy(self, **kw):
        return RecordPolicy(**kw)

    def test_not_active_before_begin(self):
        p = self._policy()
        self.assertFalse(p.active)
        self.assertEqual(p.check(T0), RECORD_CONTINUE)
        self.assertEqual(p.elapsed(T0), 0.0)

    def test_person_present_keeps_recording(self):
        """人一直在画面里，就算远超 idle 阈值也不能停。"""
        p = self._policy(min_seconds=10, idle_stop_seconds=5, max_seconds=600)
        p.begin(T0)
        for i in range(1, 40):
            now = T0 + timedelta(seconds=i * 3)
            p.note(now, has_person=True)
            self.assertEqual(p.check(now), RECORD_CONTINUE, f"第 {i} 帧不该停")

    def test_stops_after_person_leaves(self):
        p = self._policy(min_seconds=10, idle_stop_seconds=5, max_seconds=600)
        p.begin(T0)
        # 先录够地板时长
        p.note(T0 + timedelta(seconds=10), has_person=True)
        # 人走
        p.note(T0 + timedelta(seconds=12), has_person=False)
        self.assertEqual(p.check(T0 + timedelta(seconds=14)), RECORD_CONTINUE)
        self.assertEqual(p.check(T0 + timedelta(seconds=18)), RECORD_STOP_IDLE)

    def test_min_seconds_is_a_floor(self):
        """地板期内画面空了也守住——漏检一帧不该把录像掐断。"""
        p = self._policy(min_seconds=20, idle_stop_seconds=2, max_seconds=600)
        p.begin(T0)
        p.note(T0 + timedelta(seconds=1), has_person=False)
        self.assertEqual(p.check(T0 + timedelta(seconds=15)), RECORD_CONTINUE)
        # 过了地板，空闲够久，才允许停
        self.assertEqual(p.check(T0 + timedelta(seconds=25)), RECORD_STOP_IDLE)

    def test_max_seconds_beats_everything(self):
        """天花板必须压住「人还在」，否则永远停不下来。"""
        p = self._policy(min_seconds=10, idle_stop_seconds=5, max_seconds=30)
        p.begin(T0)
        now = T0 + timedelta(seconds=31)
        p.note(now, has_person=True)  # 人还在
        self.assertEqual(p.check(now), RECORD_STOP_MAX)

    def test_max_stops_even_below_min(self):
        """max 小于 min 这种怪配置下，也要以 max 为准。"""
        p = self._policy(min_seconds=100, idle_stop_seconds=5, max_seconds=10)
        p.begin(T0)
        self.assertEqual(p.check(T0 + timedelta(seconds=11)), RECORD_STOP_MAX)

    def test_person_comes_back_resets_idle(self):
        """中途离开又回来，空闲计时要重新算。"""
        p = self._policy(min_seconds=5, idle_stop_seconds=10, max_seconds=600)
        p.begin(T0)
        p.note(T0 + timedelta(seconds=10), has_person=False)
        p.note(T0 + timedelta(seconds=15), has_person=True)  # 回来了
        self.assertEqual(p.check(T0 + timedelta(seconds=24)), RECORD_CONTINUE)
        self.assertEqual(p.check(T0 + timedelta(seconds=26)), RECORD_STOP_IDLE)

    def test_end_returns_to_reusable_state(self):
        p = self._policy()
        p.begin(T0)
        self.assertTrue(p.active)
        p.end()
        self.assertFalse(p.active)
        self.assertEqual(p.check(T0), RECORD_CONTINUE)
        # 还能再开一段
        p.begin(T0 + timedelta(seconds=100))
        self.assertTrue(p.active)

    def test_note_ignored_when_not_recording(self):
        p = self._policy()
        p.note(T0, has_person=True)
        self.assertFalse(p.active)

    def test_idle_for_when_person_never_seen(self):
        """开录第一帧就没见着人时，空闲按从头算，不能崩。"""
        p = self._policy()
        p.begin(T0, has_person=False)
        self.assertEqual(p.idle_for(T0 + timedelta(seconds=9)), 9.0)

    def test_finish_accumulates_total(self):
        p = self._policy(min_seconds=0, idle_stop_seconds=5, max_seconds=600)
        p.begin(T0)
        secs = p.finish(T0 + timedelta(seconds=12))
        self.assertEqual(secs, 12.0)
        self.assertEqual(p.total_recorded, 12.0)
        self.assertFalse(p.active)

    def test_continuing_keeps_total_new_event_resets_it(self):
        p = self._policy(min_seconds=0, idle_stop_seconds=5, max_seconds=600)
        p.begin(T0)
        p.finish(T0 + timedelta(seconds=10))
        self.assertEqual(p.total_recorded, 10.0)

        p.begin(T0 + timedelta(seconds=20), continuing=True)
        self.assertEqual(p.total_recorded, 10.0, "接力段要接着累计")

        p.begin(T0 + timedelta(seconds=30), continuing=False)
        self.assertEqual(p.total_recorded, 0.0, "新的一次在场要清零")

    def test_exhausted_ends_the_chain(self):
        """总额度用完就不再续写——防止人在屋里待一天把磁盘写满。"""
        p = self._policy(
            min_seconds=0, idle_stop_seconds=5, max_seconds=10, max_total_seconds=25
        )
        p.begin(T0)
        p.finish(T0 + timedelta(seconds=10))       # 累计 10
        self.assertFalse(p.exhausted)
        p.begin(T0 + timedelta(seconds=10), continuing=True)
        p.finish(T0 + timedelta(seconds=20))       # 累计 20
        self.assertFalse(p.exhausted)
        p.begin(T0 + timedelta(seconds=20), continuing=True)
        p.finish(T0 + timedelta(seconds=30))       # 累计 30 >= 25
        self.assertTrue(p.exhausted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
