"""语音提示：检测到人时，对现场的人说一句话。

## 为什么要"先合成再播放"，而不是直接 say()

改这一版之前，播报走的是 pyttsx3 的实时 `say()`。实测下来它在
`volume=1.0` 时输出峰值只有 **-11.8 dBFS**——引擎自己浪费了约 12 dB
的动态范围，这就是"声音太小"的真正原因。改 config 里的 volume 没用，
因为它本来就顶到 1.0 了。

所以现在的流程是：

    合成一次（SAPI5 → WAV）→ 做响度处理 → 缓存 → 每次播缓存文件

好处有三：
1. **真的更响**：实测峰值 +10.8 dB、RMS +14.1 dB（见 loudness.py 注释）
2. **不卡主循环**：合成和音频处理只在第一次做（约 1 秒），之后每次
   播报是瞬时开始；而且 winsound 是系统级的异步播放
3. **可核验**：缓存目录里留着处理后的 WAV 和处理报告，
   "变响了多少"是能翻出来看的数字，不是一句"我调过了"

## 两个必须注意的点

1. **必须异步播**。播一句大约 2.8 秒，如果在主循环里同步播，
   这两三秒摄像头就不读了——人可能正好在这段时间里走过去。
2. **队列长度设为 1**。现场的人不需要听三遍重复播报，
   正在播的时候新来的请求直接丢弃。

完全离线：走 Windows SAPI5 + winsound，不联网、不需要 API key。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
from typing import Optional, Tuple

import loudness

log = logging.getLogger("room-watch")

DEFAULT_TEXT = "您已进入监控区域，请注意"


def _choose_voice(engine, needle: str = "Chinese") -> str:
    """尽量挑一个中文语音，挑不到就用系统默认。返回选中的名字。"""
    try:
        for v in engine.getProperty("voices"):
            if needle.lower() in v.name.lower():
                engine.setProperty("voice", v.id)
                return v.name
    except Exception as exc:
        log.warning("挑选语音失败，使用默认: %s", exc)
    return "系统默认"


def render_raw(text: str, cfg: Optional[dict] = None, out_path: Optional[str] = None) -> Optional[str]:
    """把文案合成成 WAV，**不做任何响度处理**。

    单独抽出来是为了能 A/B 对比：`--say-raw` 听原始版，`--say` 听处理版，
    两者差别有多大，耳朵直接判断，不用信我的一面之词。
    """
    cfg = cfg or {}
    try:
        import pyttsx3
    except ImportError:
        log.error("未安装 pyttsx3，语音提示不可用。pip install pyttsx3")
        return None
    try:
        engine = pyttsx3.init()
    except Exception as exc:
        log.error("语音引擎初始化失败: %s", exc)
        return None

    engine.setProperty("rate", int(cfg.get("rate", 170)))
    engine.setProperty("volume", float(cfg.get("volume", 1.0)))
    _choose_voice(engine, str(cfg.get("voice_contains", "Chinese")))

    if out_path is None:
        import tempfile

        out_path = os.path.join(tempfile.gettempdir(), "room-watch-raw.wav")
    engine.save_to_file(text, out_path)
    engine.runAndWait()

    # SAPI5 是异步落盘的，等文件长齐再返回，否则会读到半截
    for _ in range(20):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return out_path
        time.sleep(0.1)
    return out_path if os.path.exists(out_path) else None


def _play_sync(path: str) -> bool:
    """同步播放一个 WAV（等它放完才返回）。只用于命令行试听。"""
    try:
        import winsound

        winsound.PlaySound(path, winsound.SND_FILENAME)
        return True
    except Exception as exc:
        log.error("播放失败: %s", exc)
        print(f"播放失败: {exc}")
        return False


def play_raw(cfg: Optional[dict] = None) -> None:
    """试听未处理的原始语音。"""
    cfg = cfg or {}
    print("=== 原始语音（未做响度处理，就是现在的线上效果）===")
    raw = render_raw(cfg.get("text", DEFAULT_TEXT), cfg)
    if not raw:
        print("合成失败")
        return
    x, sr = loudness.read_wav(raw)
    print(f"峰值 {loudness.peak_dbfs(x):.1f} dBFS / 响度 {loudness.rms_dbfs(x):.1f} dBFS")
    _play_sync(raw)


def ab_compare(cfg: Optional[dict] = None) -> None:
    """先放原始版，停一下，再放处理版。用耳朵判断差别。"""
    cfg = dict(cfg or {})
    text = cfg.get("text", DEFAULT_TEXT)
    print("=== 对比试听：先原始版，再处理版 ===")
    raw = render_raw(text, cfg)
    if not raw:
        print("合成失败")
        return
    x, sr = loudness.read_wav(raw)
    processed, report = loudness.process(x, sr, cfg.get("loudness") or {})
    loud_path = os.path.join(os.path.dirname(raw), "room-watch-loud.wav")
    loudness.write_wav(loud_path, processed, sr)

    print(f"① 原始版：峰值 {report['before_peak_dbfs']} dBFS / 响度 {report['before_rms_dbfs']} dBFS")
    _play_sync(raw)
    time.sleep(0.8)
    print(f"② 处理版：峰值 {report['after_peak_dbfs']} dBFS / 响度 {report['after_rms_dbfs']} dBFS"
          f"（峰值 {report['gain_peak_db']:+.1f} dB / 响度 {report['gain_rms_db']:+.1f} dB）")
    _play_sync(loud_path)
    print("对比结束。")


class Speaker:
    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.text = cfg.get("text", DEFAULT_TEXT)
        # volume 只影响「合成时的原始音量」，已经是 1.0 上限；
        # 真正的响度由 loudness 段决定
        self.volume = float(cfg.get("volume", 1.0))
        self.rate = int(cfg.get("rate", 170))
        self.voice_contains = str(cfg.get("voice_contains", "Chinese"))

        self.loudness_cfg = dict(cfg.get("loudness") or {})
        self.cache_dir = str(cfg.get("cache_dir", "voice_cache"))
        self.use_cached_clip = bool(cfg.get("preprocess", True))

        self._q: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self.last_report: Optional[dict] = None
        self.last_source = ""          # "cached" | "live"（回退）
        self._clips: dict = {}         # 文案 -> (wav 路径, 时长秒)

        if self.enabled:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ 对外接口

    def say(self, text: Optional[str] = None) -> bool:
        """把一句话丢给后台线程。不会阻塞主循环。"""
        if not self.enabled:
            return False
        msg = text or self.text
        try:
            self._q.put_nowait(msg)
            return True
        except queue.Full:
            # 正在播，丢弃即可——没必要让人连听三遍
            self._dropped += 1
            return False

    def shutdown(self) -> None:
        self._stop.set()
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)  # 立刻掐断正在播的
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ 后台线程

    def _worker(self) -> None:
        ready = self._prepare(self.text)  # 预热：启动时就把音频做好
        if ready is not None:
            log.info("语音已就绪：%.2f 秒，峰值 %.1f dBFS（原始 %.1f dBFS）",
                     ready[2]["seconds"], ready[2]["after_peak_dbfs"],
                     ready[2]["before_peak_dbfs"])
            try:
                self._loop_cached()
                return
            except Exception as exc:
                # winsound 只在 Windows 有；导入失败或播放失败都不能让线程
                # 静默死掉——那比声音小严重得多（监控看着在跑，其实已经哑了）
                log.warning("预处理播放不可用（%s），回退到实时合成", exc)
        else:
            log.warning("语音预处理不可用，回退到实时合成（音量会偏小）")
        self._loop_live()

    def _loop_cached(self) -> None:
        import winsound  # 非 Windows 会在这里抛 ImportError，由 _worker 兜住

        while not self._stop.is_set():
            text = self._dequeue()
            if text is None:
                continue
            clip = self._clips.get(text) or self._prepare(text)
            if clip is None:
                self._speak_live_once(text)
                continue
            path, secs, _ = clip
            try:
                log.info("播放语音提示（预处理版）: %s", text)
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            except Exception as exc:
                log.warning("异步播放失败，改用实时合成: %s", exc)
                self._speak_live_once(text)
                continue
            self._wait(secs)

    def _loop_live(self) -> None:
        while not self._stop.is_set():
            text = self._dequeue()
            if text is None:
                continue
            self._speak_live_once(text)

    def _dequeue(self) -> Optional[str]:
        try:
            return self._q.get(timeout=0.5)
        except queue.Empty:
            return None

    def _wait(self, seconds: float) -> None:
        """等音频播完，但保持可中断（否则 Ctrl+C 要等两三秒才响应）。"""
        deadline = time.monotonic() + max(0.0, seconds - 0.1)
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.05)

    # ------------------------------------------------------------ 预处理

    def _prepare(self, text: str) -> Optional[Tuple[str, float, dict]]:
        """合成 + 做响 + 缓存。失败返回 None（由调用方回退到实时合成）。"""
        if text in self._clips:
            p, s, r = self._clips[text]
            return (p, s, r)
        if not self.use_cached_clip:
            return None

        try:
            wav_path = self._clip_path(text)
        except Exception as exc:
            log.warning("准备语音缓存目录失败: %s", exc)
            return None

        report_path = wav_path[:-4] + ".json"
        try:
            if os.path.exists(wav_path) and os.path.exists(report_path):
                with open(report_path, "r", encoding="utf-8") as f:
                    rep = json.load(f)
                clip = (wav_path, float(rep.get("seconds", 2.5)), rep)
                self._clips[text] = clip
                self.last_report = rep
                self.last_source = "cached"
                return clip

            raw = self._synth_to_temp(text)
            if raw is None:
                return None
            samples, rate = loudness.read_wav(raw)
            processed, rep = loudness.process(samples, rate, self.loudness_cfg)
            loudness.write_wav(wav_path, processed, rate)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2)

            clip = (wav_path, float(rep["seconds"]), rep)
            self._clips[text] = clip
            self.last_report = rep
            self.last_source = "cached"
            log.info("语音响度处理完成：%s", "；".join(loudness.report_lines(rep)))
            return clip
        except Exception as exc:
            log.warning("语音预处理失败: %s", exc)
            return None

    def _clip_path(self, text: str) -> str:
        """缓存文件名 = 影响音色与音量的所有参数的哈希。

        这样改文案、改语速、改响度参数都会自动重新生成，
        不会出现"改了配置却还播旧音频"这种鬼问题。
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        key = "|".join([
            text,
            str(self.rate),
            f"{self.volume:.2f}",
            self.voice_contains,
            json.dumps(self.loudness_cfg, sort_keys=True, ensure_ascii=False),
        ])
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return os.path.abspath(os.path.join(self.cache_dir, f"{digest}.wav"))

    def _synth_to_temp(self, text: str) -> Optional[str]:
        """用 SAPI5 把文案合成成一个临时 WAV（复用了模块级的 render_raw）。"""
        out = self._clip_path(text)[:-4] + ".raw.wav"
        return render_raw(text, {
            "rate": self.rate,
            "volume": self.volume,
            "voice_contains": self.voice_contains,
        }, out)

    def _pick_voice(self, engine) -> None:
        name = _choose_voice(engine, self.voice_contains)
        log.info("已选择语音: %s", name)

    # ------------------------------------------------------------ 回退路径

    def _speak_live_once(self, text: str) -> None:
        """回退：pyttsx3 实时合成播放（响度不如预处理版，但保证能出声）。"""
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)
            engine.setProperty("volume", self.volume)
            self._pick_voice(engine)
            log.info("播放语音提示（实时合成）: %s", text)
            engine.say(text)
            engine.runAndWait()
            self.last_source = "live"
        except Exception as exc:
            log.error("播放失败: %s", exc)


def selftest(play: bool = True) -> None:
    """不依赖摄像头，单独验证语音链路，并把响度增益量出来。"""
    print("=== 语音链路自检 ===")
    raw = render_raw(DEFAULT_TEXT)
    if not raw:
        print("合成失败：检查 pyttsx3 是否安装、系统是否有可用语音")
        return

    x, sr = loudness.read_wav(raw)
    processed, report = loudness.process(x, sr, {})
    print(f"原始音频  : 峰值 {report['before_peak_dbfs']} dBFS / "
          f"响度 {report['before_rms_dbfs']} dBFS")
    print(f"处理后    : 峰值 {report['after_peak_dbfs']} dBFS / "
          f"响度 {report['after_rms_dbfs']} dBFS")
    print(f"提升      : 峰值 {report['gain_peak_db']:+.1f} dB / "
          f"响度 {report['gain_rms_db']:+.1f} dB")

    if play:
        loud_path = os.path.join(os.path.dirname(raw), "room-watch-loud.wav")
        loudness.write_wav(loud_path, processed, sr)
        print("正在播放处理后的音频...")
        if _play_sync(loud_path):
            print(f"播放完成。文件留在 {loud_path}")


if __name__ == "__main__":
    selftest()
