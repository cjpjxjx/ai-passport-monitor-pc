"""索引画面：帧的切条、压缩与打包。

画面为 256 色调色板索引，每像素 1 字节，按条带独立 deflate 压缩。
几何见 GEOMETRIES，默认 320x180、每条带 12 行、共 15 条带。

视频包载荷格式（长度为大端，单位为其后压缩数据的字节数）：

    [u8 首条带][u8 条带数][u16 长度 * 条带数][压缩条带]

长度为 0 的条带表示未变化，设备不重绘。
"""

from __future__ import annotations

import os
import struct
import zlib

# 画面几何：名称 -> (宽, 高, 每条带行数)。必须与固件编译时的几何一致，
# 否则设备在 CONFIG 校验时拒绝会话。四项均为 15 条带；320x180 为 16:9 默认值，其余为 4:3。
GEOMETRIES = {"320x240": (320, 240, 16), "280x210": (280, 210, 14),
              "240x180": (240, 180, 12), "320x180": (320, 180, 12)}
_NAME = os.environ.get("TV_GEOMETRY", "320x180")
if _NAME not in GEOMETRIES:
    raise SystemExit(f"TV_GEOMETRY={_NAME} is not a geometry this firmware has: "
                     f"choose one of {', '.join(sorted(GEOMETRIES))}")
WIDTH, HEIGHT, STRIPE_ROWS = GEOMETRIES[_NAME]
STRIPES = HEIGHT // STRIPE_ROWS
STRIPE_PIXELS = WIDTH * STRIPE_ROWS
FRAME_PIXELS = WIDTH * HEIGHT

# ffmpeg 输出 rgb8：固定 3-3-2 网格即调色板（见 LiveChannel.build_palette），
# 每帧恰为 FRAME_PIXELS 字节，无调色板尾部。
TRAILER_BYTES = 0
FRAME_BYTES = FRAME_PIXELS + TRAILER_BYTES

# 视频包上限，必须等于固件 AV_VIDEO_MAX；超限会话被结束，不会被截断。
VIDEO_MAX = 22528

# 打包目标，刻意低于 VIDEO_MAX：单包过大会使读取期间音频无人读取，
# 见 CLAUDE.md 关键设计决策 3。
PACKET_TARGET_BYTES = int(os.environ.get("TV_PACKET_TARGET", "12288"))

# 必须等于固件 AV_PALETTE_ENTRIES。
PALETTE_ENTRIES = 256

_HEADER = struct.Struct(">BB")

# deflate 级别，见 CLAUDE.md 关键设计决策 2。
_COMPRESS_LEVEL = 6


def compress_stripes(frame: bytes, send: list[bool] | None = None) -> list[bytes]:
    """切条并逐条压缩；`send` 为 False 的条带输出空串（长度 0，设备不重绘）。"""
    if len(frame) != FRAME_PIXELS:
        raise ValueError(f"frame is {len(frame)} bytes, expected {FRAME_PIXELS}")
    return [
        zlib.compress(frame[at * STRIPE_PIXELS:(at + 1) * STRIPE_PIXELS], _COMPRESS_LEVEL)
        if send is None or send[at] else b""
        for at in range(STRIPES)
    ]


# 增量发送：只发相对设备已有画面有变化的条带。TV_DELTA=0 关闭。
# 依赖固件接受零长度条带，旧固件遇到会结束会话。
# 变化量按差异索引数衡量，不比对相等；差异占比不超过 DELTA_MAX_DIFF 的条带不发，
# 其余按差异数从大到小竞争字节预算。
DELTA = os.environ.get("TV_DELTA", "1") != "0"
DELTA_MAX_DIFF = float(os.environ.get("TV_DELTA_MAX_DIFF", "0.01"))


def _ladder() -> list[bytes]:
    """从粗到细排列的 3-3-2 索引映射表，[0] 为无损；每档把各颜色通道舍入到步长的倍数。"""
    def table(rg: int, b: int) -> bytes:
        def snap(v: int, top: int, step: int) -> int:
            return min(top, (v + step // 2) // step * step)
        return bytes((snap(i >> 5, 7, rg) << 5) | (snap((i >> 2) & 7, 7, rg) << 2)
                     | snap(i & 3, 3, b) for i in range(256))
    return [bytes(range(256))] + [table(rg, b) for rg, b in
                                  ((2, 1), (3, 1), (3, 2), (4, 2), (7, 3))]


LADDER = _ladder()


def encode_within(raw: bytes, shown: bytes | None, tick: int, target: int,
                  min_diff: float = DELTA_MAX_DIFF) -> tuple[list[bytes], bytes, int]:
    """压缩 `raw` 使其不超过 `target` 字节，返回（待发条带，设备将显示的画面，档位）。

    先原样尝试，再逐档降质，取第一个放得下的档位；最粗仍放不下才按变化量丢条带。
    """
    chosen, drawn = [], raw
    for rung, table in enumerate(LADDER):
        drawn = raw.translate(table) if rung else raw
        chosen = choose_stripes(drawn, shown, tick, 1 << 30, min_diff)
        if _wire_size(chosen) <= target:
            return chosen, drawn, rung
    return (choose_stripes(drawn, shown, tick, target, min_diff), drawn,
            len(LADDER) - 1)


def _wire_size(chosen: list[bytes]) -> int:
    return _HEADER.size + 2 * len(chosen) + sum(map(len, chosen))


def differing_pixels(a: bytes, b: bytes) -> int:
    """统计两帧不同的索引个数：异或后数零字节，只用标准库。"""
    x = int.from_bytes(a, "big") ^ int.from_bytes(b, "big")
    return len(a) - x.to_bytes(len(a), "big").count(0)


def _deflate(raw: bytes, at: int) -> bytes:
    return zlib.compress(raw[at * STRIPE_PIXELS:(at + 1) * STRIPE_PIXELS], _COMPRESS_LEVEL)


def choose_stripes(raw: bytes, shown: bytes | None, tick: int, allowed: int,
                   min_diff: float = DELTA_MAX_DIFF) -> list[bytes]:
    """在 `allowed` 字节内选出值得发送的压缩条带；未选中的条带为空串。

    `shown` 为设备已收到的画面，None 表示首帧，全部发送。
    按 `tick` 轮转强制刷新一条，不计入 `allowed`，用于修复设备丢失的条带。
    其余条带按与 `shown` 的差异数从大到小发送，直到放不下为止；落选条带
    无需记账，下一帧差异只增不减。
    """
    if len(raw) != FRAME_PIXELS:
        raise ValueError(f"frame is {len(raw)} bytes, expected {FRAME_PIXELS}")
    if shown is None:
        return compress_stripes(raw)
    floor = int(STRIPE_PIXELS * min_diff)
    refresh = tick % STRIPES
    out = [b""] * STRIPES
    out[refresh] = _deflate(raw, refresh)
    spent = _HEADER.size + 2 * STRIPES + len(out[refresh])
    ranked = []
    for at in range(STRIPES):
        if at == refresh:
            continue
        lo, hi = at * STRIPE_PIXELS, (at + 1) * STRIPE_PIXELS
        changed = differing_pixels(raw[lo:hi], shown[lo:hi])
        if changed > floor:
            ranked.append((-changed, at))
    ranked.sort()
    for _, at in ranked:
        z = _deflate(raw, at)
        if spent + len(z) > allowed:
            continue
        out[at] = z
        spent += len(z)
    return out


def fill_stripes(raw: bytes, shown: bytes | None, chosen: list[bytes],
                 allowed: int) -> list[bytes]:
    """在 `chosen` 之外，用 `allowed` 的剩余字节补发被跳过且有差异的条带，差异大者优先。"""
    if shown is None:
        return chosen
    spent = _wire_size(chosen)
    ranked = []
    for at in range(STRIPES):
        if chosen[at]:
            continue
        lo, hi = at * STRIPE_PIXELS, (at + 1) * STRIPE_PIXELS
        changed = differing_pixels(raw[lo:hi], shown[lo:hi])
        if changed:
            ranked.append((-changed, at))
    ranked.sort()
    out = list(chosen)
    for _, at in ranked:
        z = _deflate(raw, at)
        if spent + len(z) > allowed:
            continue
        out[at] = z
        spent += len(z)
    return out


def apply_stripes(raw: bytes, shown: bytes | None, sent: list[bytes]) -> bytes:
    """设备收到 `sent` 后显示的画面：`shown` 上覆盖 `raw` 中对应的条带。"""
    merged = bytearray(shown if shown is not None else raw)
    for at, stripe in enumerate(sent):
        if stripe:
            lo = at * STRIPE_PIXELS
            merged[lo:lo + STRIPE_PIXELS] = raw[lo:lo + STRIPE_PIXELS]
    return bytes(merged)


def packet(first: int, compressed: list[bytes]) -> bytes:
    """由连续的压缩条带构造一个载荷。"""
    if not compressed:
        raise ValueError("a packet carries at least one stripe")
    if first + len(compressed) > STRIPES:
        raise ValueError(f"stripes {first}+{len(compressed)} run past the frame")
    table = struct.pack(f">{len(compressed)}H", *(len(s) for s in compressed))
    payload = _HEADER.pack(first, len(compressed)) + table + b"".join(compressed)
    if len(payload) > VIDEO_MAX:
        raise ValueError(f"packet is {len(payload)} bytes, over the {VIDEO_MAX} limit")
    return payload


def frame_packets(frame: bytes) -> list[bytes]:
    """整帧压缩并打包。"""
    return pack_stripes(compress_stripes(frame))


def pack_stripes(compressed: list[bytes]) -> list[bytes]:
    """按字节预算把已压缩条带打包成多个载荷；空条带占 2 字节长度表。

    单条带超过预算时独自成包，超过 VIDEO_MAX 则抛错。
    """
    packets: list[bytes] = []
    run: list[bytes] = []
    run_bytes = 0
    at = 0
    for stripe in compressed:
        # 长度表每条带增加 2 字节，预算先扣除包头与长度表。
        overhead = _HEADER.size + 2 * (len(run) + 1)
        if run and overhead + run_bytes + len(stripe) > PACKET_TARGET_BYTES:
            packets.append(packet(at, run))
            at += len(run)
            run, run_bytes = [], 0
            overhead = _HEADER.size + 2
        if overhead + len(stripe) > VIDEO_MAX:
            raise ValueError(
                f"stripe {at + len(run)} is {len(stripe)} bytes and does not fit "
                f"in a {VIDEO_MAX}-byte packet even alone")
        run.append(stripe)
        run_bytes += len(stripe)
    if run:
        packets.append(packet(at, run))
    return packets


def read_exactly(stream, size: int) -> bytes:
    """从管道读 `size` 字节，管道结束时可能不足；管道单次读取可能返回短数据，故循环。"""
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_frame(stream, stop) -> bytes | None:
    """从 rawvideo 管道读一帧；流结束或只读到半帧时返回 None，半帧不补齐。"""
    if stop.is_set():
        return None
    raw = read_exactly(stream, FRAME_PIXELS)
    if len(raw) != FRAME_PIXELS:
        return None
    if TRAILER_BYTES:
        if len(read_exactly(stream, TRAILER_BYTES)) != TRAILER_BYTES:
            return None
    return raw


# --- 载荷解包（测试与校验用） ---

def unpack(payload: bytes) -> list[bytes]:
    """把载荷拆成解压后的条带。"""
    if len(payload) < _HEADER.size:
        raise ValueError("payload shorter than its own header")
    first, count = _HEADER.unpack_from(payload)
    if count == 0 or first + count > STRIPES:
        raise ValueError(f"stripe range {first}+{count} outside the frame")
    end = _HEADER.size + 2 * count
    if len(payload) < end:
        raise ValueError("payload truncated inside its length table")
    lengths = struct.unpack_from(f">{count}H", payload, _HEADER.size)
    if sum(lengths) != len(payload) - end:
        raise ValueError("length table does not match the bytes that follow")
    out = []
    at = end
    for n, length in enumerate(lengths):
        raw = zlib.decompress(payload[at:at + length])
        if len(raw) != STRIPE_PIXELS:
            raise ValueError(f"stripe {first + n} is {len(raw)} bytes, not {STRIPE_PIXELS}")
        out.append(raw)
        at += length
    return out


def palette_bytes(rgb24: bytes) -> bytes:
    """把 256 个 RGB 三元组转成设备要的大端 RGB565。

    ffmpeg 的 3-3-2 网格按 36 与 85 倍乘，量化到 RGB565 后与下面的移位结果一致。
    """
    if len(rgb24) < PALETTE_ENTRIES * 3:
        raise ValueError("a palette is 256 RGB triples")
    out = bytearray()
    for i in range(PALETTE_ENTRIES):
        r, g, b = rgb24[3 * i], rgb24[3 * i + 1], rgb24[3 * i + 2]
        out += struct.pack(">H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return bytes(out)


# --- 缩放与补边 ---

# 取色样与出图共用同一条滤镜串，保证二者看到的是同一幅补边后的画面。
SCALE_MODES = ("bicubic", "lanczos", "bilinear", "area", "neighbor")
SCALE = os.environ.get("TV_SCALE", "").strip() or "bicubic"
if SCALE not in SCALE_MODES:
    raise SystemExit(f"TV_SCALE={SCALE!r} 无效，可选值：{' / '.join(SCALE_MODES)}")
SCALER_FLAGS = SCALE
FIT = (f"scale=iw*sar:ih,setsar=1,"
       f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags={SCALER_FLAGS},"
       f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1")

DEFAULT_USER_AGENT = "AptvPlayer-UA"


def input_options(url: str, user_agent: str = "", paced: bool = False) -> list[str]:
    """读取源的 ffmpeg 输入参数。

    reconnect 选项仅网络源可用，本地文件会因未知选项而打不开。
    """
    options: list[str] = []
    if paced:
        # -re 按原生速率读入；关掉初始突发，否则开头一批帧同时到达。
        options += ["-re", "-readrate_initial_burst", "0", "-flags", "low_delay"]
    if url.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        options += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]
    if os.environ.get("TV_STREAM_LOOP") == "1":
        options += ["-stream_loop", "-1"]
    effective_ua = user_agent or (DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
    if effective_ua:
        options += ["-user_agent", effective_ua]
    return options


