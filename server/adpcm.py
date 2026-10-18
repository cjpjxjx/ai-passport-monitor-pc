"""FAV1 音频包的 IMA ADPCM 编码。

一个块为 4 字节头加每采样一个 4 位码，每对采样中较早的在低半字节。头部是第一个采样
之前的编码器状态：大端 s16 预测值、u8 步长索引、一个零字节。解码端每块从头部重新开始，
丢块不会影响下一块。与 WAV 布局不同，WAV 把采样 0 放在头部。
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8)
INDEX_MAX = len(STEP_TABLE) - 1
HEADER_BYTES = 4


class Encoder:
    """跨块保存预测值与步长索引。"""

    def __init__(self, predictor: int = 0, index: int = 0):
        self.predictor = predictor
        self.index = index

    def encode(self, samples: Sequence[int]) -> bytes:
        """把偶数个采样编码为一个块，头部在前。"""
        if len(samples) % 2:
            raise ValueError("a block holds an even number of samples")
        header = struct.pack(">hBB", self.predictor, self.index, 0)
        pred, index = self.predictor, self.index
        codes = []
        for sample in samples:
            step = STEP_TABLE[index]
            diff = sample - pred
            sign = 0
            if diff < 0:
                sign = 8
                diff = -diff
            code = 0
            delta = step >> 3
            if diff >= step:
                code = 4
                diff -= step
                delta += step
            step >>= 1
            if diff >= step:
                code |= 2
                diff -= step
                delta += step
            step >>= 1
            if diff >= step:
                code |= 1
                delta += step
            pred += -delta if sign else delta
            pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
            index += INDEX_TABLE[code]
            index = 0 if index < 0 else INDEX_MAX if index > INDEX_MAX else index
            codes.append(code | sign)
        self.predictor, self.index = pred, index
        packed = bytes(low | high << 4 for low, high in zip(codes[0::2], codes[1::2]))
        return header + packed

    def encode_pcm(self, pcm: bytes) -> bytes:
        """编码小端有符号 16 位单声道 PCM。"""
        if len(pcm) % 4:
            raise ValueError("PCM must hold an even number of 16-bit samples")
        return self.encode(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def decode(block: bytes, count: int) -> list[int]:
    """解码一个块的前 `count` 个采样，作为编码器的对照实现。"""
    pred, index, zero = struct.unpack_from(">hBB", block)
    if zero or index > INDEX_MAX:
        raise ValueError("invalid block header")
    out = []
    for at in range(count):
        byte = block[HEADER_BYTES + at // 2]
        code = byte >> 4 if at % 2 else byte & 0x0F
        step = STEP_TABLE[index]
        delta = step >> 3
        if code & 4:
            delta += step
        if code & 2:
            delta += step >> 1
        if code & 1:
            delta += step >> 2
        pred += -delta if code & 8 else delta
        pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
        index += INDEX_TABLE[code & 7]
        index = 0 if index < 0 else INDEX_MAX if index > INDEX_MAX else index
        out.append(pred)
    return out
