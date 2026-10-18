"""发送时序常量与固定调色板。"""

from __future__ import annotations

import os

from . import frames

# CONFIG 中声明的名义帧率。设备只校验其合理（1..30），不按它调度：每帧自带时间戳。
# 实际帧率由源决定（文件由 ffprobe 探测，桌面捕捉固定 30）；显式设置 `TV_FPS` 则覆盖文件的探测。
FPS = int(os.environ.get("TV_FPS", "25"))
AUDIO_CHUNK_MS = 40
# CONFIG 的 start_delay_ms，固件只接受该值。
START_DELAY_MS = 200


def default_palette() -> bytes:
    """画面索引所指的固定 3-3-2 调色板。

    步长 36 与 85 同 ffmpeg 的 AV_PIX_FMT_RGB8，量化到 RGB565 后 256 项全部一致。
    """
    return frames.palette_bytes(bytes(
        value
        for k in range(256)
        for value in (36 * ((k >> 5) & 7), 36 * ((k >> 2) & 7), 85 * (k & 3))
    ))
