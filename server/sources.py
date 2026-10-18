"""无限画面源：没有文件时长，会话由用户中断结束。"""

from __future__ import annotations

from . import hwaccel
from .protocol import Tracks

# 与设备 fps 上限一致（main/av_player.c，1..30）。
CAPTURE_FPS = 30


class DesktopSource:
    """Windows 桌面捕捉：ffmpeg ddagrab 抓取指定显示器，含鼠标指针。

    ffmpeg 只产出画面；声音由 `server/loopback.py` 在进程内产生。
    `audio` 为 True 时带默认输出设备的声音，否则只有画面。
    """

    label = "desktop"
    fps = CAPTURE_FPS
    # ddagrab 输出 D3D11 硬件帧，下载到内存后才能进入软件滤镜。
    video_prefix = "hwdownload,format=bgra"
    # 位于 format=rgb8 之前：该转换由此处的缩放器完成，关闭其抖动，静止区域的相邻帧才逐字节相同，增量发送才能跳过。
    video_suffix = "scale=flags=bicubic:sws_dither=none"
    # 硬件加速：在显存中缩小后再下载，见 server/hwaccel.py。
    gpu_prefix = hwaccel.QSV_DESKTOP_PREFIX

    def __init__(self, monitor: int = 0, audio: bool = False):
        self.monitor = monitor
        self.audio = audio

    @property
    def tracks(self) -> Tracks:
        return Tracks(audio=self.audio, video=True)

    def input_args(self) -> list[str]:
        """ffmpeg 输入参数，替代文件源的 `-i <path>`。"""
        return ["-f", "lavfi", "-i", f"ddagrab=framerate={self.fps}:output_idx={self.monitor}"]
