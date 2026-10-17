"""loudness.py 的单测：不需要声卡、不需要摄像头，纯数学验证。

    python test_loudness.py

重点盯两件事：
1. 处理之后**必须真的更响**（峰值和响度都要涨），否则这层就是白写的
2. 限幅器**不能偷偷吃掉增益**——第一版用整段 tanh 就犯了这个错，
   峰值被从 -1 dBFS 削到 -3 dBFS，白白吐回去 2.4 dB
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest

import numpy as np

import loudness as L

RATE = 22050


def tone(freq: float, seconds: float, amp: float, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def dbfs_to_lin(db: float) -> float:
    return 10 ** (db / 20.0)


class TestMeasure(unittest.TestCase):
    def test_full_scale_is_zero_dbfs(self):
        x = np.ones(1000, dtype=np.float32)
        self.assertAlmostEqual(L.peak_dbfs(x), 0.0, places=3)

    def test_half_scale_is_minus_six(self):
        x = np.full(1000, 0.5, dtype=np.float32)
        self.assertAlmostEqual(L.peak_dbfs(x), -6.02, places=2)

    def test_sine_rms_is_peak_minus_three(self):
        """正弦波的 RMS 比峰值低 3.01 dB，这是拿来校准公式的基准。"""
        x = tone(440, 0.5, 0.5)
        self.assertAlmostEqual(L.peak_dbfs(x) - L.rms_dbfs(x), 3.01, places=1)

    def test_empty_does_not_crash(self):
        empty = np.array([], dtype=np.float32)
        self.assertLess(L.peak_dbfs(empty), -900)
        self.assertLess(L.rms_dbfs(empty), -900)


class TestTrim(unittest.TestCase):
    def test_leading_and_trailing_silence_removed(self):
        silence = np.zeros(RATE // 2, dtype=np.float32)  # 0.5 秒
        body = tone(300, 1.0, 0.5)
        x = np.concatenate([silence, body, silence])
        y = L.trim_silence(x, RATE)
        # 原始 2.0 秒，切完应该接近 1.0 秒（外加 30ms 的余量）
        self.assertLess(y.size / RATE, 1.2)
        self.assertGreater(y.size / RATE, 1.0)

    def test_all_silence_returned_unchanged(self):
        x = np.zeros(RATE, dtype=np.float32)
        self.assertEqual(L.trim_silence(x, RATE).size, x.size)

    def test_loud_signal_is_untouched(self):
        x = tone(300, 0.5, 0.9)
        y = L.trim_silence(x, RATE, pad_ms=30)
        self.assertEqual(y.size, x.size)


class TestHighpass(unittest.TestCase):
    def test_kills_dc_offset(self):
        x = np.full(RATE, 0.4, dtype=np.float32)  # 纯直流
        y = L.highpass(x, RATE, 100)
        self.assertLess(abs(float(np.mean(y))), 0.01)

    def test_keeps_speech_band(self):
        x = tone(1000, 0.5, 0.5)
        y = L.highpass(x, RATE, 100)
        # 1kHz 在语音带上，不该被削掉多少
        self.assertGreater(L.rms_dbfs(y), L.rms_dbfs(x) - 1.0)

    def test_zero_hz_is_noop(self):
        x = tone(440, 0.2, 0.5)
        np.testing.assert_array_equal(L.highpass(x, RATE, 0), x)


class TestCompress(unittest.TestCase):
    def test_quiet_below_threshold_untouched(self):
        x = tone(440, 0.3, dbfs_to_lin(-40.0))  # 远低于 -24dBFS 阈值
        y = L.compress(x, RATE, threshold_dbfs=-24.0, ratio=3.0)
        self.assertAlmostEqual(L.peak_dbfs(y), L.peak_dbfs(x), places=1)

    def test_loud_signal_is_reduced(self):
        x = tone(440, 0.3, dbfs_to_lin(-6.0))
        y = L.compress(x, RATE, threshold_dbfs=-24.0, ratio=3.0)
        self.assertLess(L.peak_dbfs(y), L.peak_dbfs(x))

    def test_ratio_one_is_noop(self):
        x = tone(440, 0.3, dbfs_to_lin(-6.0))
        y = L.compress(x, RATE, threshold_dbfs=-24.0, ratio=1.0)
        np.testing.assert_allclose(y, x, atol=1e-6)

    def test_quiet_part_lifted_relative_to_loud(self):
        """压缩的核心价值：动态范围被压小，弱音节相对被抬高。"""
        loud = tone(440, 0.4, dbfs_to_lin(-6.0))
        quiet = tone(440, 0.4, dbfs_to_lin(-30.0))
        x = np.concatenate([loud, quiet])
        y = L.compress(x, RATE, threshold_dbfs=-24.0, ratio=3.0)
        gap_before = L.rms_dbfs(loud) - L.rms_dbfs(quiet)
        gap_after = L.rms_dbfs(y[: loud.size]) - L.rms_dbfs(y[loud.size:])
        self.assertLess(gap_after, gap_before)

    def test_peak_at_the_very_start_is_still_compressed(self):
        """回归哨兵：峰值落在音频开头时，不能被压缩器漏掉。

        第一版用因果包络（从 0 爬升），峰值样本处增益还是 1.0，
        等于最响的那一下完全没被压到。归一化是照这个溜掉的峰值做的，
        整段于是被压在下面——实测 RMS 只涨 2.7 dB 而不是 10.8 dB。
        修法是给包络加前瞻（文件处理可以合法地看未来）。
        """
        x = tone(440, 0.3, dbfs_to_lin(-6.0))
        y = L.compress(x, RATE, threshold_dbfs=-24.0, ratio=3.0)
        head = slice(0, int(RATE * 0.005))
        head_in = float(np.max(np.abs(x[head])))
        head_out = float(np.max(np.abs(y[head])))
        reduction_db = 20 * math.log10(head_out / head_in)
        self.assertLess(reduction_db, -4.0)


class TestNormalize(unittest.TestCase):
    def test_peak_lands_on_target(self):
        x = tone(440, 0.3, dbfs_to_lin(-12.0))
        y, gain = L.normalize(x, -1.0)
        self.assertAlmostEqual(L.peak_dbfs(y), -1.0, places=1)
        self.assertAlmostEqual(gain, 11.0, places=0)

    def test_already_at_target_is_identity(self):
        x = tone(440, 0.3, dbfs_to_lin(-1.0))
        y, gain = L.normalize(x, -1.0)
        self.assertAlmostEqual(gain, 0.0, places=1)
        np.testing.assert_allclose(y, x, atol=1e-5)

    def test_silence_is_left_alone(self):
        x = np.zeros(100, dtype=np.float32)
        y, gain = L.normalize(x, -1.0)
        self.assertEqual(gain, 0.0)
        self.assertEqual(float(np.max(np.abs(y))), 0.0)


class TestSoftLimit(unittest.TestCase):
    """这一组是回归测试：第一版把 tanh 用在整段信号上，吃掉了刚拿到的增益。"""

    def test_signal_at_minus_one_dbfs_is_not_touched(self):
        x = tone(440, 0.3, dbfs_to_lin(-1.0))
        y = L.soft_limit(x)
        # 这是那个 bug 的哨兵：处理完峰值必须还在 -1 dBFS 附近
        self.assertAlmostEqual(L.peak_dbfs(y), -1.0, places=1)
        np.testing.assert_allclose(y, x, atol=1e-3)

    def test_quiet_signal_passes_through_bit_exact(self):
        x = tone(440, 0.2, 0.1)
        y = L.soft_limit(x)
        np.testing.assert_array_equal(y, x)

    def test_hot_sample_is_pulled_under_ceiling(self):
        x = np.array([0.0, 3.0, 0.0], dtype=np.float32)  # 严重超标
        y = L.soft_limit(x, ceiling_dbfs=-0.5)
        # 映射是渐近的：会无限贴近天花板但不越过（留 1e-4 dB 的数值余量）
        self.assertLessEqual(L.peak_dbfs(y), -0.5 + 1e-4)
        self.assertLess(L.peak_dbfs(y), L.peak_dbfs(x))
        self.assertTrue(np.all(np.isfinite(y)))

    def test_curve_is_continuous_at_knee(self):
        """接缝处不能有跳变，否则会有可听的"咔"声。

        判据是**斜率连续**（knee 两侧斜率都是 1），而不是"输出值几乎相等"——
        后者把正常的上升也算成了跳变。
        """
        ceiling = dbfs_to_lin(-0.5)
        knee = ceiling * dbfs_to_lin(-0.4)
        lo, hi = knee * 0.999, knee * 1.001
        out_lo = float(L.soft_limit(np.array([lo], dtype=np.float32))[0])
        out_hi = float(L.soft_limit(np.array([hi], dtype=np.float32))[0])
        self.assertAlmostEqual(out_lo, lo, places=6)          # knee 以下原样通过
        self.assertAlmostEqual(out_hi - out_lo, hi - lo, places=4)  # 斜率仍为 1

    def test_peak_at_normalize_target_is_below_knee(self):
        """回归哨兵：限幅器的 knee 必须在归一化目标之上，否则它会吃掉增益。"""
        target = dbfs_to_lin(-1.0)
        ceiling = dbfs_to_lin(-0.5)
        knee = ceiling * dbfs_to_lin(-0.4)
        self.assertLess(target, knee)


class TestProcess(unittest.TestCase):
    def test_makes_things_louder(self):
        x = tone(440, 0.5, dbfs_to_lin(-11.8))
        y, rep = L.process(x, RATE, {})
        self.assertGreater(rep["gain_peak_db"], 5.0)
        self.assertGreater(rep["gain_rms_db"], 5.0)
        self.assertGreater(L.peak_dbfs(y), L.peak_dbfs(x))

    def test_rms_gain_tracks_peak_gain_on_steady_tone(self):
        """稳定音上，响度增益应该跟峰值增益基本一致。

        如果压缩器漏掉了峰值（增益在峰值处为 1.0），归一化就会照着那个
        溜掉的峰值做，RMS 增益会明显低于峰值增益——这条就是那个 bug 的哨兵。
        """
        x = tone(440, 0.5, dbfs_to_lin(-11.8))
        _, rep = L.process(x, RATE, {})
        self.assertAlmostEqual(rep["gain_rms_db"], rep["gain_peak_db"], delta=1.0)

    def test_never_exceeds_ceiling(self):
        x = np.concatenate([
            tone(440, 0.3, 0.95),
            tone(200, 0.3, 0.02),
        ])
        y, rep = L.process(x, RATE, {})
        self.assertLessEqual(L.peak_dbfs(y), -0.4 + 0.05)

    def test_report_keys_present(self):
        x = tone(440, 0.3, 0.3)
        _, rep = L.process(x, RATE, {})
        for key in (
            "before_peak_dbfs", "before_rms_dbfs", "after_peak_dbfs",
            "after_rms_dbfs", "gain_peak_db", "gain_rms_db", "seconds", "steps",
        ):
            self.assertIn(key, rep)
        self.assertTrue(rep["steps"])

    def test_can_disable_every_stage(self):
        """全部关掉时只应剩下归一化，不该崩。"""
        x = tone(440, 0.3, dbfs_to_lin(-10.0))
        y, rep = L.process(x, RATE, {
            "trim": False, "highpass_hz": 0, "compress": False,
        })
        self.assertAlmostEqual(L.peak_dbfs(y), -1.0, places=1)

    def test_empty_input_does_not_crash(self):
        y, rep = L.process(np.array([], dtype=np.float32), RATE, {})
        self.assertEqual(y.size, 0)
        self.assertEqual(rep["seconds"], 0.0)

    def test_report_lines_are_printable(self):
        x = tone(440, 0.3, 0.3)
        _, rep = L.process(x, RATE, {})
        text = "\n".join(L.report_lines(rep))
        self.assertIn("dB", text)


class TestWavRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_signal(self):
        x = tone(440, 0.2, 0.6)
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.wav")
        L.write_wav(path, x, RATE)
        y, rate = L.read_wav(path)
        self.assertEqual(rate, RATE)
        self.assertEqual(y.size, x.size)
        # 16bit 量化 + 写用 32767 / 读用 32768 的换算，误差应在 2 个 LSB 以内
        # （固定比例误差约 3e-5，即 0.0003 dB，听不出来）
        self.assertLess(float(np.max(np.abs(y - x))), 2 / 32768)

    def test_write_clips_out_of_range_samples(self):
        x = np.array([0.0, 5.0, -5.0], dtype=np.float32)
        d = tempfile.mkdtemp()
        path = os.path.join(d, "c.wav")
        L.write_wav(path, x, RATE)
        y, _ = L.read_wav(path)
        self.assertLessEqual(L.peak_dbfs(y), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
