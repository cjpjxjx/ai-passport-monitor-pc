"""把本地视频文件推送给设备的单客户端局域网服务器，由 ffmpeg 转码。"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import os
import secrets
import select
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import enum
from typing import Any, Dict, List

class SessionState(str, enum.Enum):
    PLAYING = "PLAYING"
    CLOSED = "CLOSED"

from . import frames, hwaccel, netident, tonemap
from .sources import DesktopSource
from .live import LiveChannel, LiveError
# 时序常量统一来自 media.py，live.py 同样从那里导入。
from .media import AUDIO_CHUNK_MS, FPS, START_DELAY_MS
from .protocol import (IO_TIMEOUT, VIDEO_CONTINUES, Kind, Packet,
                       ProtocolError, Tracks, json_bytes, json_object, receive_packet,
                       send_packet, _write_slice)

# 固件 CONFIG 校验的全部字段；两端必须一致，改动须与固件同时进行。
# 按轨道分组：只有声明了某轨，才带该轨的字段，设备也只校验声明了的轨。
CONFIG_AUDIO = {"sample_rate": 16000, "channels": 1, "sample_bits": 16,
                "audio_codec": "ima_adpcm", "audio_chunk_ms": AUDIO_CHUNK_MS}
CONFIG_VIDEO = {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": FPS,
                "video_max_bytes": frames.VIDEO_MAX,
                # 设备据此校验自身几何；两端不一致时拒绝通信，避免条带画到错误的行。
                "stripe_rows": frames.STRIPE_ROWS}


def build_config(session: int, tracks: Tracks, fps: int | None = None) -> dict:
    """按会话携带的轨道构造 CONFIG；`fps` 覆盖画面轨的名义帧率。"""
    config = {"audio": tracks.audio, "video": tracks.video,
              "start_delay_ms": START_DELAY_MS, "session": session}
    if tracks.audio:
        config.update(CONFIG_AUDIO)
    if tracks.video:
        config.update(CONFIG_VIDEO)
        if fps is not None:
            config["fps"] = fps
    return config

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))

# 单帧写入时限（秒），超时即放弃会话；须小于设备约 3 秒无数据即断开的阈值。
FRAME_WRITE_DEADLINE_S = float(os.environ.get("TV_FRAME_DEADLINE_S", "2.5"))

# 套接字发送缓冲区大小；见 CLAUDE.md 关键设计决策 4。
SEND_BUFFER_BYTES = int(os.environ.get("TV_SEND_BUFFER", "16384"))

# 画面包每次写入的分片大小；分片之间让音频先发，见 CLAUDE.md 核心设计原则 2。
VIDEO_SLICE_BYTES = 4096

# 单次写入超过该秒数时记录其大小与所在位置；0 表示每次都记录。
PROBE_SLOW_S = float(os.environ.get("TV_PROBE_SLOW_S", "0.05"))

# 解码器持续没有任何输出超过该秒数，视为卡死并结束会话。
STALL_TIMEOUT_S = 10


WILDCARD_BIND = "0.0.0.0"


def local_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return any(ip in network for network in PRIVATE_NETWORKS)


def authenticate(packet: Packet) -> None:
    """校验首个 HELLO 的形状与版本；配对令牌暂不校验。"""
    if (packet.kind != Kind.HELLO or packet.session != 0 or
            packet.seq != 0 or packet.pts_ms != 0):
        raise ProtocolError("expected initial HELLO")
    hello = json_object(packet.payload)
    if type(hello.get("version")) is not int or hello["version"] != 1:
        raise ProtocolError("unsupported HELLO version")


class AVServer:
    """单线程负责接入与收发，多余连接直接关闭；阻塞操作均带时限，每 50 ms 轮询停止标志。"""

    def __init__(self, path: str, bind: str = "127.0.0.1",
                 port: int = 8096, logger=None, source=None):
        if bind != WILDCARD_BIND and not local_ipv4(bind):
            raise ValueError("bind must be 0.0.0.0 or a loopback or RFC1918 IPv4 address")
        self.path = path
        # 无限源（server/sources.py）；None 表示 path 是本地文件。
        self.source = source
        self.bind, self.port = bind, port
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.listener: socket.socket | None = None
        self.completed = self.rejected = self.failed = self.dropped_video = 0
        # 握手阶段即关闭的连接（设备休眠、端口探测）单独计数，不计入 failed。
        self.abandoned = 0
        self.audio_sent = self.video_sent = 0
        self.session_id = 0
        self.phase = "idle"
        self.live_channel: LiveChannel | None = None
        self.live_note = ""
        self.ffmpeg = "ffmpeg"
        # 硬件加速模式（hwaccel.MODES），已由 hwaccel.resolve 探测，会话内不再探测。
        self.hwaccel = "none"
        # 文件源是否做色调映射（tonemap.probe 通过才开），启动时确定。
        self.tone_map = False
        # 可注入，便于不依赖 ffmpeg 和网络测试发送循环。
        self.channel_factory = LiveChannel
        self.logger = logger or (lambda message: None)
        self.session_state: SessionState = SessionState.CLOSED
        self.state_history: List[Dict[str, Any]] = []

    def set_session_state(self, state: SessionState, session: int, reason: str = "", **kwargs: Any) -> None:
        """记录会话状态迁移及当时的发送计数。"""
        old_state = getattr(self, "session_state", SessionState.CLOSED)
        self.session_state = state
        now_m = time.monotonic()
        rec = {
            "timestamp": round(now_m, 4),
            "session": session,
            "from_state": old_state.value if isinstance(old_state, SessionState) else str(old_state),
            "to_state": state.value if isinstance(state, SessionState) else str(state),
            "reason": reason,
            "audio_sent": getattr(self, "audio_sent", 0),
            "video_sent": getattr(self, "video_sent", 0),
            "dropped_video": getattr(self, "dropped_video", 0),
            **kwargs
        }
        if not hasattr(self, "state_history") or self.state_history is None:
            self.state_history = []
        self.state_history.append(rec)
        detail_str = " ".join(f"{k}={v}" for k, v in kwargs.items())
        self.logger(
            f"session={session} STATE_CHANGE {rec['from_state']} -> {rec['to_state']} "
            f"reason='{reason}' {detail_str}".strip()
        )

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.port))
            self.port = listener.getsockname()[1]
            # 设备每秒重试一次；服务单个连接期间需留有积压队列，否则重试被直接拒绝。
            listener.listen(8)
            listener.setblocking(False)
            self.ready.set()
            while not self.stop.is_set():
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                connection, peer = listener.accept()
                with connection:
                    if not local_ipv4(peer[0]):
                        self.rejected += 1
                        continue
                    connection.setblocking(False)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    # 发送缓冲区只需容纳一个画面包；见 CLAUDE.md 关键设计决策 4。
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                          SEND_BUFFER_BYTES)

                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    self.session_id = self.audio_sent = self.video_sent = 0
                    outcome, reason = "closed", "none"
                    self.phase = "hello_receive"
                    try:
                        self._session(connection)
                        self.completed += 1
                        # 播完一次即退出，否则设备每秒重连会把文件从头再播。
                        self.stop.set()
                    except (OSError, EOFError, ProtocolError, LiveError) as error:
                        # 写入可能不完整，必须关闭连接；日志只含异常类名，不含载荷、令牌、对端地址。
                        # 鉴权之前断开计为放弃，之后断开计为失败。
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        # 不在此补采 ffmpeg 诊断：_live_session 的 finally 已记录并清空 live_channel。
                    except Exception as error:
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        import traceback
                        tb = traceback.extract_tb(error.__traceback__)
                        sanitized_tb = " -> ".join([f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in tb[-3:]])
                        self.live_note = f"unexpected={type(error).__name__}[{sanitized_tb}]"
                    finally:
                        # 仅记录代码内定义的阶段名与异常类名，不记录 str/repr(error)。
                        note = f" note={self.live_note}" if self.live_note else ""
                        self.logger(f"session={self.session_id} state={outcome} "
                                    f"reason={reason} phase={self.phase} "
                                    f"authenticated={int(self.session_id != 0)} "
                                    f"audio_sent={self.audio_sent} video_sent={self.video_sent}{note}")
                        self.live_note = ""
                # 拒绝旧会话期间积压的连接，不影响后续会话。
                self._reject_waiting()
        self.listener = None

    def _reject_waiting(self) -> None:
        assert self.listener is not None
        # 即使连接洪泛也限定处理次数。
        for _ in range(8):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            connection.close()
            self.rejected += 1

    def _device_left(self, connection: socket.socket, session: int) -> bool:
        """设备已结束本会话时返回 True：发来 END，或直接关闭连接。

        写入前及一帧的多次写入之间调用，使正常换频道不被计为错误。收到 END 以外的包抛 ProtocolError；
        无 END 的断开（EOFError）原样抛出，由 serve() 按握手是否完成分类。
        """
        if not select.select([connection], [], [], 0)[0]:
            return False
        try:
            packet = receive_packet(connection, IO_TIMEOUT, expected_session=session)
        except TimeoutError:
            # select 报告可读但数据不足一个完整包：设备仍在，继续会话。
            return False
        except EOFError:
            # 无 END 的断开原样抛出，由 serve() 分类。
            raise
        if packet.kind != Kind.END:
            raise ProtocolError("only END allowed while playing")
        return True

    def _live_session(self, connection: socket.socket) -> None:
        """握手后启动转码进程并发送媒体；每个会话独占一个转码进程。"""
        self.phase = "live_start"
        channel = self.channel_factory(
            self.path, self.ffmpeg, source=self.source, hwaccel_mode=self.hwaccel, tone_map=self.tone_map,
            log=lambda message: self.logger(f"session={self.session_id} {message}"))
        # CONFIG 须声明轨道，故先探测源有哪些轨道；探测很快，不采样画面，也尚未启动任何资源。
        tracks = channel.probe()
        try:
            session = secrets.randbelow(0xFFFFFFFF) + 1
            self.session_id = session
            # 先发 CONFIG 再向源请求数据：设备握手只给约 2 秒，先采样调色板会使握手超时。
            # CONFIG 只描述格式，与调色板无关。
            config = build_config(session, tracks,
                                  self.source.fps if self.source is not None else None)
            send_packet(connection, Packet(Kind.CONFIG, session, 0, 0, json_bytes(config)))
            self.phase = "live_palette"
            # 首帧前必须有调色板，缺失则设备无色可用，画面全黑。
            try:
                channel.build_palette()
            except LiveError as error:
                # 明确告知设备失败，否则它会一直等待调色板。
                try:
                    send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                   json_bytes({"reason": "source unavailable"})))
                except OSError:
                    pass
                raise LiveError(str(error)) from None
            if not channel.palette:
                raise LiveError("channel has no palette")
            # start() 放在 try 内，Popen 失败时不泄漏管道与 stderr 文件；失败即结束会话。
            channel.start()
            self.live_channel = channel
            # PALETTE 紧随 CONFIG、先于所有帧；每个会话发一次。
            send_packet(connection, Packet(Kind.PALETTE, session, 1, 0,
                                           channel.palette))
            self.logger(f"session={session} state=live file={os.path.basename(self.path)}")
            self.set_session_state(SessionState.PLAYING, session, reason="session_started")
            self._pace_live(connection, channel, session)
        finally:
            self.set_session_state(SessionState.CLOSED, session, reason="session_closed",
                                   audio_sent=self.audio_sent, video_sent=self.video_sent)
            try:
                if channel.failure() is not None:
                    try:
                        self.live_note = channel.diagnostics()
                    except Exception as diag_err:
                        self.logger(f"session={session} WARNING: channel diagnostics failed: {diag_err}")
            finally:
                try:
                    channel.close()
                except Exception as close_err:
                    self.logger(f"session={session} WARNING: channel close failed: {close_err}")
                finally:
                    self.live_channel = None

    def _send_audio(self, connection: socket.socket, channel: LiveChannel,
                    session: int, deadline: float | None = None) -> bool:
        """发送队列中已有的音频块（至多 32 块），返回是否发出了内容。

        时间戳是已发块数乘块长，设备要求每块恰为上一块加一个块长。
        """
        sent = False
        for _ in range(32):
            if not select.select([], [connection], [], 0)[1]:
                break
            block = channel.pop_audio()
            if block is None:
                break
            self.phase = "live_pcm_send"
            send_packet(connection, Packet(Kind.PCM, session, self._seq,
                                           self.audio_sent * AUDIO_CHUNK_MS, block),
                        deadline=deadline)
            self._seq += 1
            self.audio_sent += 1
            sent = True
        return sent

    def _send_frame(self, connection: socket.socket, channel: LiveChannel,
                    session: int, packets: list[bytes]) -> bool:
        """发送一帧的全部包；设备已离开时返回 False。

        整帧共用一个写入时限。画面包之间让已到的音频先发：单个包内字节必须连续，
        包与包之间可以插入音频包，音频不因画面而欠载。
        """
        deadline = time.monotonic() + max(1.0 / channel.fps, FRAME_WRITE_DEADLINE_S)
        # 有音频轨时画面时间戳取发送时已发音频的位置；否则取帧序号乘帧间隔。固件要求画面时间戳严格递增。
        if channel.tracks.audio:
            position = self.audio_sent * AUDIO_CHUNK_MS
        else:
            position = self._frames_sent * 1000 // channel.fps
        stamp = max(position, self._video_wire + 1)
        self._video_wire = stamp
        self._frames_sent += 1
        for n, part in enumerate(packets):
            self.phase = "control_receive"
            if self._device_left(connection, session):
                return False
            self._send_audio(connection, channel, session, deadline)
            self.phase = "live_video_send"
            # 同一帧各包时间戳相同，除首包外以 VIDEO_CONTINUES 标志声明，否则设备按时间戳不前进而拒绝。
            wire = Packet(Kind.JPEG, session, self._seq, stamp, part,
                          VIDEO_CONTINUES if n else 0).encode()
            sent = 0
            while sent < len(wire):
                data = memoryview(wire)[sent:sent + VIDEO_SLICE_BYTES]
                started = time.monotonic()
                if not _write_slice(connection, data, deadline):
                    raise TimeoutError("video packet deadline expired")
                took = time.monotonic() - started
                self._worst_write = max(self._worst_write, took)
                if took > PROBE_SLOW_S:
                    self.logger(f"PROBE slice n={n} bytes={len(data)} took_ms={took * 1000:.0f}")
                sent += len(data)
            self._seq += 1
            self.video_sent += 1
            self._video_bytes += len(wire)
        return True

    def _pace_live(self, connection: socket.socket, channel: LiveChannel,
                   session: int) -> None:
        """按到达顺序发送：音频块一到就发，画面发最新一帧。

        不预缓冲、不做内容时间配对，两个队列只有几百毫秒，落后时丢最旧的，延迟因此有界。
        音频优先于画面；设备按自己的音频时钟呈现画面，画面到达即画。
        """
        # CONFIG 占序号 0，PALETTE 为 1，首个媒体包为 2；设备拒绝非连续序号。
        self._seq = 2
        self._video_wire = -1
        self._frames_sent = 0
        self._worst_write = 0.0
        self._video_bytes = 0
        started = last_report = time.monotonic()
        last_audio = last_video = 0
        try:
            while not self.stop.is_set():
                if channel.failure() is not None:
                    raise LiveError("decoder stopped")
                # 写之前先读：设备换页面时发 END 并立即关闭，先写会在帧中途得到 broken pipe。
                readable, writable, _ = select.select([connection], [connection], [], 0)
                if readable:
                    self.phase = "control_receive"
                    if self._device_left(connection, session):
                        return
                sent = False
                if writable:
                    sent = self._send_audio(connection, channel, session)
                    packets = channel.pop_video()
                    if packets is not None:
                        if not self._send_frame(connection, channel, session, packets):
                            return
                        sent = True
                if not sent:
                    if channel.finished():
                        # 源读完且内容全部发出：告知设备结束，设备会把已排队的声音播完。
                        self.phase = "live_end"
                        send_packet(connection, Packet(Kind.END, session, self._seq, 0))
                        return
                    if channel.stalled(STALL_TIMEOUT_S):
                        raise LiveError("decoder produced nothing")
                    self.phase = "schedule_wait"
                    channel.wake.wait(0.005)
                    channel.wake.clear()
                now = time.monotonic()
                if now - last_report >= 5:
                    elapsed = now - last_report
                    self.logger(
                        f"live t={now - started:6.1f}s fps={channel.fps} "
                        f"audio_q={len(channel.audio):3d} video_q={len(channel.video):3d} "
                        f"audio_pps={(self.audio_sent - last_audio) / elapsed:5.1f} "
                        f"video_pps={(self.video_sent - last_video) / elapsed:5.1f} "
                        f"video_kBps={self._video_bytes / elapsed / 1000:5.1f} "
                        f"dropped_video={channel.dropped_video} dropped_audio={channel.dropped_audio} "
                        f"worst_write_ms={self._worst_write * 1000:4.0f}")
                    last_report = now
                    last_audio, last_video = self.audio_sent, self.video_sent
                    self._video_bytes = 0
                    self._worst_write = 0.0
            # 服务端停止是正常结束，不是流故障；无限源没有读完的时刻，用户中断时告知设备结束。
            if self.source is not None:
                try:
                    send_packet(connection, Packet(Kind.END, session, self._seq, 0))
                except (OSError, TimeoutError) as error:
                    self.logger(f"session={session} WARNING: END not delivered: {type(error).__name__}")
        finally:
            self.dropped_video = channel.dropped_video

    def _session(self, connection: socket.socket) -> None:
        self.phase = "hello_receive"
        hello = receive_packet(connection, expected_session=0)
        self.phase = "authenticate"
        authenticate(hello)
        self._live_session(connection)


def print_where_to_connect(bind: str, port: int) -> None:
    """启动后最后打印的内容：设备应填的地址，再给一条提示。"""
    shown = bind
    if bind == WILDCARD_BIND:
        detected = netident.lan_address()
        shown = detected if detected and local_ipv4(detected) else None
    for line in netident.describe(shown, port):
        print(line, flush=True)

    # 关闭窗口即停止服务；窗口看起来像日志，用户不易联想到。
    print("提示：关掉这个窗口，服务就停止了，设备会中断。", flush=True)


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    """play 与 capture 共用的 ffmpeg 与传输参数。"""
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--hwaccel", choices=hwaccel.MODES, default="none",
                        help="GPU acceleration: qsv = Intel iGPU, falls back to software if unavailable (default: none)")
    parser.add_argument("--bind", default=WILDCARD_BIND,
                        help="address to listen on (default: all interfaces; peers outside private networks are refused)")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--usb", metavar="PORT",
                        help="push over the device's USB serial port (e.g. COM3) instead of Wi-Fi; "
                             "the server then listens on 127.0.0.1 only")


def main(argv: list[str] | None = None) -> int:
    # 输出固定为 UTF-8：管道下默认取本地编码（Windows 中文为 GBK），log_stamp 按 UTF-8 解码会乱码。
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    play_parser = commands.add_parser("play", help="transcode a local video file in real time and push it to the device")
    play_parser.add_argument("file", help="video file to play")
    add_transport_arguments(play_parser)
    capture_parser = commands.add_parser("capture", help="capture a desktop monitor (Windows) and push it to the device, video only")
    capture_parser.add_argument("--monitor", type=int, default=0, metavar="N",
                                help="monitor index (default: 0)")
    capture_parser.add_argument("--audio", action="store_true",
                                help="also capture the default output device's sound (default: video only)")
    add_transport_arguments(capture_parser)
    args = parser.parse_args(argv)
    try:
        source = None
        if args.command == "capture":
            if sys.platform != "win32":
                print("桌面捕捉目前只支持 Windows。", file=sys.stderr)
                return 2
            if args.monitor < 0:
                print("显示器序号不能为负数。", file=sys.stderr)
                return 2
            if args.audio:
                from . import loopback
                try:
                    loopback.ensure_available()
                except RuntimeError as error:
                    print(f"无法抓取系统声音：{error}", file=sys.stderr)
                    return 2
            source = DesktopSource(args.monitor, audio=args.audio)
            target = f"{source.label}:{args.monitor}"
        else:
            if not os.path.isfile(args.file):
                print(f"文件不存在：{args.file}", file=sys.stderr)
                return 2
            target = args.file
        log = lambda message: print(message, flush=True)
        server = AVServer(target, "127.0.0.1" if args.usb else args.bind, args.port,
                          logger=log, source=source)
        server.ffmpeg = args.ffmpeg
        if source is None:
            reason = tonemap.probe(args.ffmpeg)
            server.tone_map = reason is None
            log("tonemap: libplacebo" if server.tone_map
                else f"tonemap: unavailable, HDR sources stay unmapped ({reason})")
        server.hwaccel, note = hwaccel.resolve(args.hwaccel, args.ffmpeg, target, source,
                                               tone_map=server.tone_map)
        if note:
            log(note)
        bridge_thread = None
        if args.usb:
            from .usb_bridge import UsbBridge, serial
            try:
                bridge = UsbBridge(args.usb, server, logger=log)
            except serial.SerialException as error:
                print(f"串口 {args.usb} 打不开（{type(error).__name__}）。", file=sys.stderr)
                print("确认设备已用数据线接上，且串口没有被其他程序占用。", file=sys.stderr)
                return 1
            bridge_thread = threading.Thread(target=bridge.run, daemon=True)
            bridge_thread.start()
        # 信号处理器须设置接入循环实际读取的标志。
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: server.stop.set())
        # 每个设备连接启动独立转码进程；ffmpeg 退出只结束该会话，接入循环无需重启。
        if args.usb:
            print(f"USB 串口 {args.usb}：等待设备接入。", flush=True)
        else:
            print_where_to_connect(args.bind, args.port)
        try:
            server.serve()
        finally:
            server.stop.set()
            if bridge_thread:
                bridge_thread.join(10)
        # 仅在出现异常时打印计数；握手期放弃的连接不计。
        if server.failed or server.rejected or server.dropped_video:
            print(f"已停止。出错 {server.failed} 次，拒绝 {server.rejected} 次，"
                  f"丢帧 {server.dropped_video} 次。", flush=True)
        else:
            print("已停止。", flush=True)
        return 0
    except OSError as error:
        # 绑定失败与令牌、媒体问题难以区分，端口被占用又像静默无操作；errno 不含路径与凭据。
        # 用 errno.EADDRINUSE 而非数值：各平台数值不同。
        if error.errno == errno.EADDRINUSE:
            print("端口 8096 已被占用——多半是上一次的程序还没关干净。", file=sys.stderr)
            print("把之前的窗口关掉，或重启电脑后再试。", file=sys.stderr)
        else:
            print(f"网络端口打不开（errno={error.errno}）。", file=sys.stderr)
        return 1
    except Exception:
        # 子进程错误、路径、环境变量可能含敏感值，不输出。
        print("程序没能启动。常见原因是 ffmpeg 缺失或文件无法读取；", file=sys.stderr)
        print("把上面最后几行输出发给项目的维护者可以定位。", file=sys.stderr)
        if os.environ.get("TV_TRACEBACK") == "1":
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
