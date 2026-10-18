"""桌面捕捉的声音，由本进程产生，不经 ffmpeg。

ffmpeg 同时读桌面画面与管道声音时，画面输出会攒成每秒一批，延迟随之波动；
声音单独成路，画面才能按 30 fps 逐帧到达。
`Loopback` 读取默认输出设备的 WASAPI 环回，降采样为设备要的 16 kHz 单声道，
每 40 ms 向 `sink` 交一块 640 个采样的 s16le 数据。
"""

from __future__ import annotations

import threading
import warnings
from typing import Callable

SAMPLE_RATE = 48000
CHANNELS = 2
# 一块 40 ms：48 kHz 的 1920 帧，降采样 3:1 后为设备块的 640 个采样。
BLOCK_FRAMES = 1920
DECIMATION = 3
# 抗混叠低通的抽头数；截止频率取输出奈奎斯特频率的 90%。
FIR_TAPS = 48

Sink = Callable[[bytes], None]


def ensure_available() -> None:
    """检查依赖与默认输出设备，不可用时抛 RuntimeError（附原因）。"""
    try:
        import soundcard
    except ImportError:
        raise RuntimeError("缺少依赖 soundcard，请先 pip install -r requirements.txt") from None
    try:
        soundcard.default_speaker()
    except Exception as error:
        raise RuntimeError(f"没有可用的默认声音输出设备（{type(error).__name__}）") from None


class Loopback(threading.Thread):
    """持续读取环回数据，每块降采样后交给 `sink`，直到 `stop` 置位；无声时设备仍按实时产出全零数据。"""

    def __init__(self, sink: Sink, stop: threading.Event,
                 log: Callable[[str], None] = lambda message: None):
        super().__init__(daemon=True, name="loopback-audio")
        self._sink, self._stop_event, self._log = sink, stop, log

    def run(self) -> None:
        import numpy as np
        import soundcard
        taps = np.hamming(FIR_TAPS) * np.sinc(
            0.9 / DECIMATION * (np.arange(FIR_TAPS) - (FIR_TAPS - 1) / 2))
        taps /= taps.sum()
        history = np.zeros(FIR_TAPS - 1)
        try:
            speaker = soundcard.default_speaker()
            microphone = soundcard.get_microphone(speaker.name, include_loopback=True)
            with warnings.catch_warnings():
                # 读取偶发的数据不连续只影响一个块，不必每次告警。
                warnings.simplefilter("ignore", soundcard.SoundcardRuntimeWarning)
                with microphone.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                         blocksize=BLOCK_FRAMES) as recorder:
                    while not self._stop_event.is_set():
                        block = recorder.record(numframes=BLOCK_FRAMES)
                        padded = np.concatenate((history, block.mean(axis=1)))
                        history = padded[-(FIR_TAPS - 1):]
                        filtered = np.convolve(padded, taps, mode="valid")[::DECIMATION]
                        pcm = (np.clip(filtered, -1.0, 1.0) * 32767).astype("<i2")
                        self._sink(pcm.tobytes())
        except Exception as error:
            if not self._stop_event.is_set():
                self._log(f"WARNING: loopback audio capture failed: {type(error).__name__}")
