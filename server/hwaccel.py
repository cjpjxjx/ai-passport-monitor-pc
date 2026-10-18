"""显卡硬件加速：在 GPU 上先缩小画面再下载到内存，软件路径只处理缩小后的帧。

当前只支持 Intel 核显（QSV）；探测失败时回退软件路径。
"""

from __future__ import annotations

import subprocess

MODES = ("none", "qsv")

# GPU 缩放到的宽度，高度按比例；其后的软件 FIT 完成最终的 320x180 缩放与补边。
GPU_WIDTH = 640
QSV_SCALE = f"vpp_qsv=w={GPU_WIDTH}:h=-1:format=nv12,hwdownload,format=nv12"
# 桌面捕捉：ddagrab 的 D3D11 帧映射为 QSV 帧。
QSV_DESKTOP_PREFIX = f"hwmap=derive_device=qsv,format=qsv,{QSV_SCALE}"
# 文件源：QSV 解码并保持在显存。
QSV_DECODE_ARGS = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]


def qsv_file_prefix(tone_map: bool = False) -> str:
    """文件源的 GPU 缩放前缀；要色调映射时保持 10 位（p010le），供其后的 libplacebo 使用。"""
    pix = "p010le" if tone_map else "nv12"
    return f"vpp_qsv=w={GPU_WIDTH}:h=-1:format={pix},hwdownload,format={pix}"


PROBE_TIMEOUT_S = 15.0


def resolve(mode: str, ffmpeg: str, url: str, source=None, tone_map: bool = False) -> tuple[str, str]:
    """启动时探测一次：返回实际使用的模式与一行说明；不可用时回退 "none"。

    试跑在会话外进行，设备握手只给约 2 秒，不能在会话内探测。
    """
    if mode != "qsv":
        return "none", ""
    if source is not None:
        inputs, prefix = source.input_args(), source.gpu_prefix
    else:
        inputs, prefix = [*QSV_DECODE_ARGS, "-i", url], qsv_file_prefix(tone_map)
    reason = probe(ffmpeg, inputs, prefix)
    if reason is None:
        return "qsv", "hwaccel: qsv"
    return "none", f"hwaccel: qsv unavailable, using software path ({reason})"


def probe(ffmpeg: str, inputs: list[str], prefix: str) -> str | None:
    """用真实输入与滤镜前缀试跑一帧；可用返回 None，否则返回失败原因。"""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
               *inputs, "-filter_complex", f"[0:v]{prefix}[v]",
               "-map", "[v]", "-frames:v", "1", "-f", "null", "-"]
    try:
        done = subprocess.run(command, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as error:
        return type(error).__name__
    if done.returncode != 0:
        head = done.stderr.decode(errors="replace").strip().splitlines()[:1]
        return head[0] if head else f"exit {done.returncode}"
    return None
