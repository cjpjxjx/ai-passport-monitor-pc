"""HDR 到 SDR 的色调映射：文件源缩小后交给 libplacebo（Vulkan），由它按每帧自带的色彩标记决定是否映射。

SDR 帧基本原样通过，不探测源格式。Vulkan 不可用时不做映射。
"""

from __future__ import annotations

import os
import subprocess

ALGORITHMS = ("auto", "bt.2390", "hable", "mobius", "reinhard", "spline")
ALGORITHM = os.environ.get("TV_TONEMAP", "").strip() or "auto"
if ALGORITHM not in ALGORITHMS:
    raise SystemExit(f"TV_TONEMAP={ALGORITHM!r} 无效，可选值：{' / '.join(ALGORITHMS)}")

# 放在输入之前的 ffmpeg 全局参数。
DEVICE_ARGS = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
# 色调映射前的宽度；4K 整帧上传到 Vulkan 达不到 30 fps，须先缩小。
WIDTH = 640
SOFTWARE_SCALE = f"scale={WIDTH}:-2:flags=bilinear"
MAP = (f"hwupload,libplacebo=w={WIDTH}:h=-1:format=yuv420p:colorspace=bt709:"
       f"color_primaries=bt709:color_trc=bt709:range=tv:tonemapping={ALGORITHM},"
       f"hwdownload,format=yuv420p")

PROBE_TIMEOUT_S = 15.0


def chain(gpu: bool) -> str:
    """色调映射滤镜串；`gpu` 时画面已由 QSV 缩小并以 p010le 下载，否则先在此缩小。"""
    return MAP if gpu else f"{SOFTWARE_SCALE},{MAP}"


def probe(ffmpeg: str) -> str | None:
    """用合成画面试跑 libplacebo；可用返回 None，否则返回失败原因。"""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
               *DEVICE_ARGS, "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
               "-vf", f"format=yuv420p,{MAP}", "-frames:v", "1", "-f", "null", "-"]
    try:
        done = subprocess.run(command, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as error:
        return type(error).__name__
    if done.returncode != 0:
        head = done.stderr.decode(errors="replace").strip().splitlines()[:1]
        return head[0] if head else f"exit {done.returncode}"
    return None
