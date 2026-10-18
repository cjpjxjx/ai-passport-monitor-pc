"""画面字节率：帧率在会话内固定，每帧压到 `FRAME_BYTES` 以内（见 frames.encode_within）。"""

from __future__ import annotations

import os

# TV_RATE_START 直接指定每秒字节率，不随帧率变化。
FRAME_BYTES = int(os.environ.get("TV_FRAME_BYTES", "20000"))


def start_rate(fps: int) -> int:
    """`fps` 下的固定字节率。"""
    explicit = os.environ.get("TV_RATE_START")
    return int(explicit) if explicit else FRAME_BYTES * fps
