"""bench.py 的单测：健康快照的采样与汇总，不真睡、不依赖运行中的监控。

    python tests/test_bench.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from roomwatch import bench as B


class SummarizeSamplesTest(unittest.TestCase):
    def test_mean_and_peak(self):
        samples = [
            {"cpu_pct": 40.0, "rss_mb": 300.0, "stale": False, "loop_seq": 10},
            {"cpu_pct": 60.0, "rss_mb": 400.0, "stale": True, "loop_seq": 20},
            {"cpu_pct": 50.0, "rss_mb": 350.0, "stale": False, "loop_seq": 30},
        ]
        summary = B.summarize_samples(samples)
        self.assertEqual(summary["count"], 3)
        self.assertAlmostEqual(summary["cpu_mean"], 50.0)
        self.assertAlmostEqual(summary["cpu_peak"], 60.0)
        self.assertAlmostEqual(summary["rss_mean_mb"], 350.0)
        self.assertAlmostEqual(summary["rss_peak_mb"], 400.0)
        self.assertEqual(summary["stale_samples"], 1)
        self.assertEqual(summary["frames_latest"], 30)

    def test_empty_is_all_zero(self):
        summary = B.summarize_samples([])
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["cpu_mean"], 0.0)
        self.assertEqual(summary["stale_samples"], 0)
        self.assertEqual(summary["frames_latest"], 0)

    def test_missing_fields_do_not_crash(self):
        summary = B.summarize_samples([{}, {"cpu_pct": None}])
        self.assertEqual(summary["count"], 2)
        self.assertAlmostEqual(summary["cpu_mean"], 0.0)

    def test_age_and_errors_mark_stale(self):
        samples = [
            {"cpu_pct": 1.0, "rss_mb": 2.0, "_age_seconds": 10.0,
             "consecutive_errors": 2},
            {"cpu_pct": 1.0, "rss_mb": 2.0, "_age_seconds": 0.0,
             "consecutive_errors": 0},
        ]
        summary = B.summarize_samples(samples)
        self.assertEqual(summary["stale_samples"], 1)
        self.assertEqual(summary["errors_peak"], 2)


class SampleHealthTest(unittest.TestCase):
    def test_uses_injected_clock_and_does_not_sleep_for_real(self):
        calls = {"n": 0}

        def clock():
            value = calls["n"] * 0.4
            calls["n"] += 1
            return value

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)

        samples = B.sample_health(
            "ignored", 1.0, 0.4,
            reader=lambda _path: {"cpu_pct": 1.0},
            clock=clock,
            sleep=sleep,
        )
        # start=0.0，之后 0.4、0.8 各采一次，1.2 时到点退出。
        self.assertEqual(len(samples), 2)
        self.assertEqual(sleeps, [0.4, 0.4])

    def test_empty_reader_results_are_skipped(self):
        times = iter([0.0, 0.0, 0.6, 1.1])
        samples = B.sample_health(
            "ignored", 1.0, 0.5,
            reader=lambda _path: {},
            clock=lambda: next(times),
            sleep=lambda _s: None,
        )
        self.assertEqual(samples, [])

    def test_sample_annotates_snapshot_age(self):
        times = iter([0.0, 0.0, 1.1])
        old = (datetime.now() - timedelta(seconds=10)).isoformat()
        samples = B.sample_health(
            "ignored", 1.0, 0.5,
            reader=lambda _path: {"updated_at": old},
            clock=lambda: next(times),
            sleep=lambda _s: None,
        )
        self.assertEqual(len(samples), 1)
        self.assertGreater(samples[0]["_age_seconds"], 5)


class ReadJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        self.assertEqual(B.read_json(str(Path(self.tmp) / "nope.json")), {})

    def test_corrupt_file_returns_empty(self):
        path = Path(self.tmp) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(B.read_json(str(path)), {})

    def test_valid_file_is_read(self):
        path = Path(self.tmp) / "ok.json"
        path.write_text(json.dumps({"cpu_pct": 12.5}), encoding="utf-8")
        self.assertEqual(B.read_json(str(path))["cpu_pct"], 12.5)


class CommandRegistrationTest(unittest.TestCase):
    def test_bench_command_is_registered(self):
        from roomwatch import cli

        self.assertIn("bench", cli.COMMANDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
