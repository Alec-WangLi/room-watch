"""响度处理：把 TTS 合成的音频做到「贴着满刻度但不削波」。

## 为什么需要这一层

实测 SAPI5 的中文语音（Microsoft Huihui）在 `volume=1.0` 时是这样：

    volume=1.0 -> 峰值 -11.8 dBFS, RMS -30.6 dBFS
    volume=0.3 -> 峰值 -29.3 dBFS, RMS -48.1 dBFS

注意第一行：**引擎开到最大，峰值也才 -11.8 dBFS**，离满刻度（0 dBFS）
还差将近 12 dB。这 12 dB 是引擎自己没用上的动态范围——把系统音量
拉到 100% 也补不回来，因为机器压根没把这段幅度用出来。

所以调大音量的正确做法不是改 config 里的数字（那个已经是 1.0 上限），
而是拿到音频之后**重新做一遍振幅**：

1. 切掉头尾静音  → 减少"按了没声音"的空档，也缩短占用时长
2. 峰值归一化    → 把顶点推到 -1 dBFS，拿回那 12 dB
3. 轻压缩        → 把弱的音节抬上来，人耳感知响度主要看 RMS 不是峰值
4. 软限幅        → 保证任何情况下不超过满刻度，避免削波失真

## 实现约束

- 只用 numpy + 标准库 `wave`，不引新的音频库
- **不能用 `audioop`**：Python 3.13 已经把它从标准库移除了
- 全部是纯函数，不碰声卡、不碰文件系统（读写 wav 的两个函数除外），可单测
"""

from __future__ import annotations

import math
import wave
from typing import Optional, Tuple

import numpy as np

EPS = 1e-9


# ---------------------------------------------------------------- 电平测量

def peak_dbfs(x: np.ndarray) -> float:
    """峰值电平（dBFS）。0 dBFS = 满刻度，负数表示还有多少余量。"""
    if x.size == 0:
        return -999.0
    p = float(np.max(np.abs(x)))
    return 20 * math.log10(p) if p > EPS else -999.0


def rms_dbfs(x: np.ndarray) -> float:
    """RMS 电平（dBFS）。人耳感知的响度更接近这个，而不是峰值。"""
    if x.size == 0:
        return -999.0
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20 * math.log10(r) if r > EPS else -999.0


def stats(x: np.ndarray) -> dict:
    return {
        "peak_dbfs": round(peak_dbfs(x), 1),
        "rms_dbfs": round(rms_dbfs(x), 1),
        "seconds": round(x.size / 22050.0, 2) if x.size else 0.0,  # 仅作粗略参考
    }


# ---------------------------------------------------------------- WAV 读写

def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """读成 [-1, 1] 的 float32 单声道。多声道自动混为单声道。

    手写解析而不是用 scipy/librosa：只为读一个 16bit PCM 文件不值得多一个依赖。
    """
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())

    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的采样位宽: {width * 8} bit")

    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), rate


def write_wav(path: str, x: np.ndarray, rate: int) -> None:
    """写成 16bit PCM 单声道 WAV。写之前先硬限幅，避免溢出成噪声。"""
    clipped = np.clip(x, -1.0, 1.0)
    data = (clipped * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


# ---------------------------------------------------------------- 处理环节

def trim_silence(
    x: np.ndarray,
    rate: int,
    floor_dbfs: float = -45.0,
    pad_ms: float = 30.0,
) -> np.ndarray:
    """切掉头尾的静音。

    SAPI 合成的音频两头常带一段近乎无声的垫音，播出来就是
    「按下播报 → 等半秒 → 才开始说话」。切掉它既省时间也省尴尬。
    保留 pad_ms 的余量，免得把字的起音削掉。
    """
    if x.size == 0:
        return x
    floor = 10 ** (floor_dbfs / 20.0)
    loud = np.flatnonzero(np.abs(x) > floor)
    if loud.size == 0:
        return x  # 整段都是静音，原样返回（不该发生，但别在这里崩）

    pad = int(rate * pad_ms / 1000.0)
    start = max(0, int(loud[0]) - pad)
    end = min(x.size, int(loud[-1]) + 1 + pad)
    return x[start:end]


def highpass(x: np.ndarray, rate: int, hz: float) -> np.ndarray:
    """一阶高通（用「原信号减去移动平均」近似）。

    笔记本小喇叭放不出 100Hz 以下的东西，那部分能量只会吃掉功放余量、
    让中频（也就是人声）显不出来。滤掉它等于让人声白捡一点响度。
    """
    if hz <= 0 or x.size == 0:
        return x
    n = max(1, int(rate / hz))
    if n >= x.size:
        return x
    kernel = np.ones(n, dtype=np.float32) / n
    low = np.convolve(x, kernel, mode="same")
    return (x - low).astype(np.float32)


def compress(
    x: np.ndarray,
    rate: int,
    threshold_dbfs: float = -24.0,
    ratio: float = 3.0,
    lookahead_ms: float = 20.0,
    smooth_ms: float = 15.0,
) -> np.ndarray:
    """动态压缩：超过阈值的部分按 ratio 压下去。

    为什么这一步不能省：归一化只把「最响的那一下」推到满刻度，
    而人说话时大部分音节远低于峰值——所以峰值满了，听感还是小。
    压缩把大音节压住、随后归一化再把整体抬起来，
    结果就是**弱的音节被相对抬高了**，这才是"听着更响"的来源。

    ## 为什么要前瞻，而不是"快攻慢放"

    第一版按教科书写成「快攻慢放」的因果单极点包络，结果实测发现
    增益在最响的那个样本上是 1.0（完全没压）——因为包络从 0 开始
    爬升，音频开头那几毫秒的峰值正好从压缩里溜了过去。
    归一化是照着这个溜掉的峰值做的，于是整段被压在下面，
    RMS 只涨了 2.7 dB 而不是 10.8 dB。

    文件处理不是实时处理，可以合法地「看未来」，所以这里改用
    **对称的峰值保持 + 平滑**：每个时刻的增益由它前后 lookahead_ms
    窗口内的最大电平决定。这样最响的那一下一定被压住，
    归一化之后整体才能真正抬起来。
    """
    if x.size == 0 or ratio <= 1.0:
        return x

    block = max(1, int(rate * 0.002))  # 2ms 一个块
    n = x.size
    pad = (-n) % block
    xb = np.pad(x, (0, pad)) if pad else x
    env_blocks = np.max(np.abs(xb.reshape(-1, block)), axis=1)
    nb = env_blocks.size

    # ① 对称峰值保持：取 ±W 块内的最大电平（前瞻）
    w = max(1, int(round(lookahead_ms / 1000.0 * rate / block)))
    padded = np.pad(env_blocks, w, mode="edge")
    hold = env_blocks.copy()
    for k in range(2 * w + 1):
        np.maximum(hold, padded[k: k + nb], out=hold)

    # ② 对称平滑：避免增益一条条跳（听感上叫"阶梯感"）
    s = int(round(smooth_ms / 1000.0 * rate / block))
    if s > 0:
        kern = np.full(2 * s + 1, 1.0 / (2 * s + 1), dtype=np.float32)
        env_s = np.convolve(np.pad(hold, s, mode="edge"), kern, mode="valid")
    else:
        env_s = hold

    env = np.repeat(env_s, block)[:n]
    env_db = 20.0 * np.log10(env + EPS)
    over = env_db - threshold_dbfs
    gain_db = np.where(over > 0.0, -over * (1.0 - 1.0 / ratio), 0.0)
    gain = np.power(10.0, gain_db / 20.0).astype(np.float32)
    return (x * gain).astype(np.float32)


def normalize(x: np.ndarray, target_peak_dbfs: float = -1.0) -> Tuple[np.ndarray, float]:
    """整体缩放到目标峰值，返回 (音频, 实际增益 dB)。"""
    if x.size == 0:
        return x, 0.0
    cur = peak_dbfs(x)
    if cur <= -900:
        return x, 0.0
    gain_db = target_peak_dbfs - cur
    gain = 10 ** (gain_db / 20.0)
    return (x * gain).astype(np.float32), gain_db


def soft_limit(x: np.ndarray, ceiling_dbfs: float = -0.5, knee_db: float = 0.4) -> np.ndarray:
    """最后一道保险：把顶到天花板的瞬时尖峰圆滑地压回去。

    这里有两个踩过的坑：

    1. 直接对整个信号取 tanh 是错的。tanh 在峰值附近就开始压缩，
       结果把一段已经归一到 -1 dBFS 的音频又削到 -3 dBFS，
       **白白吐回去 2.4 dB**——限幅器吃掉的就是刚费力拿回来的响度。
    2. knee 必须落在「归一化目标之上」才算真正的空转。
       归一化目标是 -1.0 dBFS，天花板 -0.5 dBFS，所以 knee 定在
       -0.9 dBFS（knee_db=0.4）：正常流程的峰值碰不到它，
       限幅器一个样本都不改；只有异常尖峰才会被圆滑地压回来。

    映射函数在 knee 处连续且斜率为 1，所以即使生效也听不出接缝。
    """
    if x.size == 0:
        return x
    ceiling = 10 ** (ceiling_dbfs / 20.0)
    knee = ceiling * (10 ** (-knee_db / 20.0))
    mag = np.abs(x)
    hot = mag > knee
    if not np.any(hot):
        return x  # 没超标就一个样本都不动

    out = x.copy()
    span = ceiling - knee
    out[hot] = np.sign(x[hot]) * (knee + span * np.tanh((mag[hot] - knee) / span))
    return out.astype(np.float32)


# ---------------------------------------------------------------- 总入口

def process(
    x: np.ndarray,
    rate: int,
    cfg: Optional[dict] = None,
) -> Tuple[np.ndarray, dict]:
    """把一段 TTS 音频做响。返回 (处理后的音频, 处理报告)。

    报告里带处理前后的实测电平，让"变响了多少"变成可核对的数字，
    而不是一句"我调大了"。
    """
    cfg = cfg or {}
    before_peak = peak_dbfs(x)
    before_rms = rms_dbfs(x)

    y = x
    steps = []

    if cfg.get("trim", True):
        n0 = y.size
        y = trim_silence(y, rate, float(cfg.get("trim_floor_dbfs", -45.0)))
        if y.size < n0:
            steps.append(f"切静音 {n0 / rate:.2f}s→{y.size / rate:.2f}s")

    hp = float(cfg.get("highpass_hz", 100.0))
    if hp > 0:
        y = highpass(y, rate, hp)
        steps.append(f"高通 {hp:.0f}Hz")

    if cfg.get("compress", True):
        y = compress(
            y,
            rate,
            threshold_dbfs=float(cfg.get("threshold_dbfs", -24.0)),
            ratio=float(cfg.get("ratio", 3.0)),
            lookahead_ms=float(cfg.get("lookahead_ms", 20.0)),
            smooth_ms=float(cfg.get("smooth_ms", 15.0)),
        )
        steps.append(f"压缩 {cfg.get('ratio', 3.0)}:1")

    y, gain_db = normalize(y, float(cfg.get("target_peak_dbfs", -1.0)))
    steps.append(f"归一化到 {cfg.get('target_peak_dbfs', -1.0)}dBFS")
    y = soft_limit(y, float(cfg.get("ceiling_dbfs", -0.5)))

    after_peak = peak_dbfs(y)
    after_rms = rms_dbfs(y)

    report = {
        "before_peak_dbfs": round(before_peak, 1),
        "before_rms_dbfs": round(before_rms, 1),
        "after_peak_dbfs": round(after_peak, 1),
        "after_rms_dbfs": round(after_rms, 1),
        "gain_peak_db": round(after_peak - before_peak, 1),
        "gain_rms_db": round(after_rms - before_rms, 1),
        "normalize_gain_db": round(gain_db, 1),
        "seconds": round(y.size / rate, 2),
        "steps": steps,
    }
    return y, report


def report_lines(report: dict) -> list:
    """把报告变成能直接打印的几行字。"""
    return [
        f"峰值  {report['before_peak_dbfs']:>7.1f} -> {report['after_peak_dbfs']:>6.1f} dBFS"
        f"   ({report['gain_peak_db']:+.1f} dB)",
        f"响度  {report['before_rms_dbfs']:>7.1f} -> {report['after_rms_dbfs']:>6.1f} dBFS"
        f"   ({report['gain_rms_db']:+.1f} dB)",
        f"时长  {report['seconds']:.2f} 秒",
        f"处理  {' / '.join(report['steps'])}",
    ]


if __name__ == "__main__":  # 手动跑：拿真实 TTS 音频量一遍
    import os
    import tempfile

    import pyttsx3

    out = os.path.join(tempfile.gettempdir(), "room-watch-loudness-test.wav")
    e = pyttsx3.init()
    for v in e.getProperty("voices"):
        if "chinese" in v.name.lower():
            e.setProperty("voice", v.id)
            break
    e.setProperty("rate", 170)
    e.save_to_file("您已进入监控区域，请注意", out)
    e.runAndWait()

    x, sr = read_wav(out)
    y, rep = process(x, sr, {})
    print("\n".join(report_lines(rep)))
