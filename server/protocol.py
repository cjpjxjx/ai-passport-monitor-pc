"""FAV1 定长限界分包；包头非法即断开连接，不做重新同步。"""

from __future__ import annotations

import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct("!4sBBHIIII")
MAGIC = b"FAV1"
VERSION = 1
# 控制包载荷上限，与固件 main/av_protocol.h 的 AV_CONTROL_MAX 一致。
CONTROL_MAX = 1024
# 视频包上限，须与固件 AV_VIDEO_MAX 一致；取自 frames.VIDEO_MAX，不重复定义。
from .frames import VIDEO_MAX
# ffmpeg 输出的一个 40 ms 块：640 个 s16le 单声道采样。
AUDIO_PCM_BYTES = 1280
# 该块上线的字节数：4 字节 IMA ADPCM 头加 640 个半字节（见 adpcm.py），须与固件 AV_AUDIO_BYTES 一致。
AUDIO_BYTES = 324
UINT32_MAX = 0xFFFFFFFF
IO_TIMEOUT = 0.25


class Kind(IntEnum):
    HELLO = 1
    CONFIG = 2
    PCM = 3
    # 画面：切成条带的一帧索引图。名称沿用 JPEG 以与固件的取值对应，载荷并非 JPEG。
    JPEG = 4
    END = 5
    ERROR = 6
    # 索引所指的 256 色调色板，每个频道首帧之前发送一次。
    PALETTE = 7

@dataclass(frozen=True)
class Tracks:
    """会话携带的轨道，由 CONFIG 的 `audio`、`video` 声明；各轨独立，至少一轨，须与固件 AV_TRACK_* 一致。"""

    audio: bool = False
    video: bool = False

    def __post_init__(self) -> None:
        if not (self.audio or self.video):
            raise ProtocolError("a session needs at least one track")


# 唯一使用的标志位，仅用于视频：本包是上一包所起始那一帧的后续条带。
# 同一帧的多个包共用一个时间戳，需要此标志与重复帧区分。
VIDEO_CONTINUES = 0x01


class ProtocolError(ValueError):
    """对端输入非法；消息中不得包含对端载荷。"""


@dataclass(frozen=True)
class Packet:
    kind: Kind
    session: int
    seq: int
    pts_ms: int
    payload: bytes = b""
    # 仅视频使用，且只能是 VIDEO_CONTINUES；用普通整数，多余的位由范围检查拦下。
    flags: int = 0

    def encode(self) -> bytes:
        validate_length(self.kind, len(self.payload))
        for value in (self.session, self.seq, self.pts_ms):
            if type(value) is not int or not 0 <= value <= UINT32_MAX:
                raise ProtocolError("integer out of range")
        if type(self.flags) is not int or not 0 <= self.flags <= 0xFF:
            raise ProtocolError("flags out of range")
        if self.flags and not (self.kind == Kind.JPEG and self.flags == VIDEO_CONTINUES):
            raise ProtocolError("flags are only for a continued video frame")
        return HEADER.pack(MAGIC, VERSION, self.kind, self.flags, self.session,
                           self.seq, self.pts_ms, len(self.payload)) + self.payload


def validate_length(kind: Kind, size: int) -> None:
    if kind in (Kind.HELLO, Kind.CONFIG, Kind.ERROR):
        valid = 0 < size <= CONTROL_MAX
    elif kind == Kind.PCM:
        valid = size == AUDIO_BYTES
    elif kind in (Kind.JPEG, Kind.PALETTE):
        valid = 0 < size <= VIDEO_MAX
    elif kind == Kind.END:
        valid = size == 0
    else:
        valid = False
    if not valid:
        raise ProtocolError("invalid payload length or type")


def json_bytes(value: dict) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    validate_length(Kind.CONFIG, len(raw))
    return raw


def json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ProtocolError("nonstandard JSON constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("invalid control JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError("control JSON must be an object")
    return value


def _wait(sock: socket.socket, deadline: float, writable: bool = False) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("packet deadline expired")
    ready = select.select([] if writable else [sock], [sock] if writable else [],
                          [], remaining)
    if not (ready[1] if writable else ready[0]):
        raise TimeoutError("packet deadline expired")


def _read_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        _wait(sock, deadline)
        try:
            chunk = sock.recv(size - len(data))
        except BlockingIOError:
            continue
        if not chunk:
            raise EOFError("peer disconnected")
        data.extend(chunk)
    return bytes(data)


def receive_packet(sock: socket.socket, timeout: float = IO_TIMEOUT,
                   expected_session: int | None = None) -> Packet:
    """包头与载荷共用一个总期限；先校验长度再分配缓冲。"""
    deadline = time.monotonic() + timeout
    raw = _read_exact(sock, HEADER.size, deadline)
    magic, version, kind, flags, session, seq, pts, length = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION:
        raise ProtocolError("unsupported packet header")
    try:
        kind = Kind(kind)
    except ValueError:
        raise ProtocolError("unknown packet type") from None
    # 与设备一致：只允许视频包带 VIDEO_CONTINUES 一个标志。
    if flags and not (kind == Kind.JPEG and flags == VIDEO_CONTINUES):
        raise ProtocolError("unsupported packet flags")
    validate_length(kind, length)
    if expected_session is not None and session != expected_session:
        raise ProtocolError("session mismatch")
    return Packet(kind, session, seq, pts, _read_exact(sock, length, deadline),
                  flags)


def send_packet(sock: socket.socket, packet: Packet,
                timeout: float = IO_TIMEOUT, deadline: float | None = None) -> None:
    """无应用层发送队列，一次最多写一个限长包，共用一个总期限。

    套接字为非阻塞。写到一半超时属于致命错误，不得在未写完的包后追加 ERROR。
    `deadline` 供同一帧的多个包共用，使整帧而不是每个包受期限约束。
    """
    data = memoryview(packet.encode())
    if deadline is None:
        deadline = time.monotonic() + timeout
    while data:
        _wait(sock, deadline, writable=True)
        try:
            sent = sock.send(data)
        except BlockingIOError:
            continue
        if not sent:
            raise EOFError("peer disconnected")
        data = data[sent:]


def _write_slice(sock: socket.socket, data: memoryview, deadline: float) -> bool:
    """写出已编码包的一个分片；期限已过时返回 False。

    调用方对包编码一次、分片多次写入，使同一循环在大视频包发送期间仍能发音频。
    超时返回 False 而不抛异常，由调用方决定整包如何处置。
    """
    remaining = memoryview(data)
    while remaining:
        try:
            _wait(sock, deadline, writable=True)
        except TimeoutError:
            return False
        try:
            sent = sock.send(remaining)
        except BlockingIOError:
            continue
        if not sent:
            raise EOFError("peer disconnected")
        remaining = remaining[sent:]
    return True
