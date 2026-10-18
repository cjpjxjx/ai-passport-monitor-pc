"""转码：单个 ffmpeg 进程解码一次，产出设备可绘制的调色板索引画面与 16 kHz 单声道 PCM。

源是实时的（本地文件以 -re 读入，桌面捕捉自身按帧率产出），因此不记录内容时间戳：
读取线程按到达顺序把音频块与画面帧放进小队列，发送端按到达时刻调度（见 tv_server._pace_live）。
画面与音频各走一条回环 TCP 连接。设备不做缩放，只解压条带并按调色板查色。
"""

from __future__ import annotations

import collections
import json
import os
import re
import socket
import subprocess
import threading
import time

from . import adpcm, frames, hwaccel, loopback, perceptual, tonemap
from .media import AUDIO_CHUNK_MS, FPS, default_palette
from .protocol import AUDIO_PCM_BYTES, Tracks
from .rate import start_rate

AUDIO_RATE = 16000
# 每个设备音频块的采样数；ffmpeg 的音频帧长固定为此值，使读取一块恰为一个设备块。
AUDIO_SAMPLES = AUDIO_PCM_BYTES // 2

# 带音频轨时画面相对其到达延后发出的毫秒数，等于声音从到达设备到被听到的时延：
# 设备音频队列（起播门槛加起播等待期间到达的块）加 I2S DMA。无声音的源不延后。
# 无音频轨的源不延后。以设备实际听感为准调整。
AV_LEAD_MS = int(os.environ.get("TV_AV_LEAD_MS", "250"))
# 音频队列容量（块数，每块 40 ms）；满时：文件源阻塞读取，ffmpeg 随之限速；无限源丢最旧块。
AUDIO_QUEUE_CHUNKS = 6
# 音频发送最多领先实时的毫秒数，即设备音频队列的目标深度。
# ffmpeg 开头会一次吐出约 0.5 秒内容，不限制则这些内容全部进入设备队列并永久保留。
AUDIO_AHEAD_MS = 80
# 画面队列在延后时长之外多留的秒数；满时同上。
VIDEO_QUEUE_SLACK_S = 0.5
# 同时到期的帧超过该数，视为发送端持续落后，跳到最新；只用于无限源（文件源不丢帧）。
MAX_DUE_FRAMES = 3

# 等待解码器回连两个回环套接字的时限（秒）。
DECODER_CONNECT_TIMEOUT_S = 10

# 追加在 frames.FIT 之后、format=rgb8 之前的 ffmpeg 视频滤镜，默认为空。
PRE_FILTER = os.environ.get("TV_PRE_FILTER", "").strip()
# 帧超出字节目标时的降质方式：perceptual.MODES 中的模式（需 numpy），或 "ladder"（逐档降色，见 frames.encode_within）。
DEGRADE_MODES = (*perceptual.MODES, "ladder")
DEGRADE = os.environ.get("TV_DEGRADE", "").strip() or "perceptual"
if DEGRADE not in DEGRADE_MODES:
    raise SystemExit(f"TV_DEGRADE={DEGRADE!r} 无效，可选值：{' / '.join(DEGRADE_MODES)}")
if DEGRADE in perceptual.MODES and not perceptual.AVAILABLE:
    raise SystemExit(f"TV_DEGRADE={DEGRADE} 需要 numpy，请先安装（pip install -r requirements.txt）")
_VIDEO_CHAIN = f"{frames.FIT},{PRE_FILTER}" if PRE_FILTER else frames.FIT
# 帧放入字节目标后剩余字节的用法：delta 只发变化超过 TV_DELTA_MAX_DIFF 的条带，
# fill 另按变化量从大到小补发被跳过的条带。
ENCODE_MODES = ("delta", "fill")
ENCODE = os.environ.get("TV_ENCODE", "").strip() or "delta"
if ENCODE not in ENCODE_MODES:
    raise SystemExit(f"TV_ENCODE={ENCODE!r} 无效，可选值：{' / '.join(ENCODE_MODES)}")

# 画面产出帧率上限，即设备的 fps 上限（main/av_player.c，1..30）；更高的源降到它之下，不拒绝。
MAX_SOURCE_FPS = 30
# ffprobe 探测的时限（秒）。
FPS_PROBE_TIMEOUT_S = float(os.environ.get("TV_FPS_PROBE_TIMEOUT_S", "5"))


class LiveError(RuntimeError):
    """转码或切包失败。"""


def source_graph(fps: int, audio: bool = False, source=None, gpu: bool = False,
                 tone_map: bool = False) -> str:
    """单个滤镜图：画面，以及文件音轨（`audio`）的音频。

    无限源（server/sources.py）的声音由本进程产生，滤镜图只有画面；
    无限源的画面先经 `source.video_prefix`（`gpu` 时为 `source.gpu_prefix`）。
    `gpu` 时文件源的画面先在 GPU 上缩小；`tone_map` 时文件源缩小后做 HDR 到 SDR 的色调映射（tonemap.py）。
    aformat=mono 须在 asetnsamples 之前，否则立体声帧含两个设备块。
    """
    if source is not None:
        head = source.gpu_prefix if gpu else source.video_prefix
    else:
        head = hwaccel.qsv_file_prefix(tone_map) if gpu else ""
    prefix = f"{head}," if head else ""
    suffix = f"{source.video_suffix}," if source is not None else ""
    mapping = f"{tonemap.chain(gpu)}," if tone_map and source is None else ""
    video = (f"[0:v]{prefix}setpts=PTS-STARTPTS,fps={fps},{mapping}{_VIDEO_CHAIN},"
             f"{suffix}format=rgb8[v]")
    if source is not None or not audio:
        return video
    pad = f"asetnsamples=n={AUDIO_SAMPLES}[a]"
    audio = f"[0:a]aresample={AUDIO_RATE},aformat=channel_layouts=mono,{pad}"
    return f"{video};{audio}"


def source_command(url: str, video_port: int, audio_port: int | None, ffmpeg: str,
                   fps: int = FPS, source=None, gpu: bool = False,
                   tone_map: bool = False) -> list[str]:
    """构造 ffmpeg 命令：读取源一次，画面与音频经回环 TCP 端口输出；没有 `audio_port` 时只输出画面。

    画面量化到固定 3-3-2 调色板（与固件 av_palette_rgb565 一致），关闭抖动；
    `-sws_dither none` 须作为输出选项，放在 `-i` 之前会被忽略。
    输出走 `tcp://127.0.0.1:<port>`，ffmpeg 为客户端，调用前父进程须已监听端口。
    """
    # 无限源按自身节拍产出，不加 -re。
    if source is not None:
        inputs = source.input_args()
    else:
        decode = hwaccel.QSV_DECODE_ARGS if gpu else []
        device = tonemap.DEVICE_ARGS if tone_map else []
        inputs = [*decode, *device, *frames.input_options(url, paced=True), "-i", url]
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning", "-nostats",
        *inputs,
        "-filter_complex", source_graph(fps, audio_port is not None, source, gpu, tone_map),
        "-map", "[v]", "-pix_fmt", "rgb8", "-sws_dither", "none",
        "-threads", "1", "-f", "rawvideo", f"tcp://127.0.0.1:{video_port}",
    ]
    if audio_port is not None:
        command += ["-map", "[a]", "-f", "s16le", f"tcp://127.0.0.1:{audio_port}"]
    return command


def parse_rate(text: str) -> float | None:
    """解析 ffprobe 的帧率文本（如 "25/1"），无法确定时返回 None。"""
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def choose_fps(avg: float | None, real: float | None) -> int | None:
    """由 ffprobe 报告的帧率选出产出帧率（整数）。

    优先用平均帧率：25 fps 节目按 50 场承载时，r_frame_rate 为 50，avg_frame_rate 为 25。
    只有容器帧率且超过上限时逐次减半，不截断到上限。
    """
    rate = avg if avg else real
    if not rate:
        return None
    while rate > MAX_SOURCE_FPS:
        rate /= 2
    return max(1, round(rate))


def _ffprobe(ffmpeg: str) -> str:
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    return probe if os.path.exists(probe) else "ffprobe"


def probe_source_fps(url: str, ffmpeg: str) -> int | None:
    """探测文件的画面帧率，未知返回 None（按名义帧率播放）。"""
    command = [_ffprobe(ffmpeg), "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=FPS_PROBE_TIMEOUT_S)
        if done.returncode != 0:
            return None
        streams = json.loads(done.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not streams:
        return None
    return choose_fps(parse_rate(streams[0].get("avg_frame_rate", "")),
                      parse_rate(streams[0].get("r_frame_rate", "")))


def probe_audio(url: str, ffmpeg: str) -> tuple[bool, float | None]:
    """文件有没有音轨，以及容器给出的时长（秒，未知为 None）。"""
    command = [_ffprobe(ffmpeg), "-v", "error", "-show_entries",
               "stream=codec_type:format=duration", "-of", "json", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=FPS_PROBE_TIMEOUT_S)
        if done.returncode != 0:
            raise LiveError("ffprobe could not read the file")
        info = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise LiveError("ffprobe failed") from None
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return has_audio, duration


class LiveChannel:
    """单个播放会话：调色板、解码器进程，以及它与发送端之间的两个小队列。"""

    def __init__(self, url: str, ffmpeg: str = "ffmpeg", source=None, log=None,
                 hwaccel_mode: str = "none", tone_map: bool = False):
        self.url = url
        self.ffmpeg = ffmpeg
        # 硬件加速模式（hwaccel.MODES），由启动时的 hwaccel.resolve 确定；gpu 在 start() 设置。
        self.hwaccel_mode = hwaccel_mode
        self.gpu = False
        # 文件源是否做色调映射，启动时由 tonemap.probe 确定。
        self.tone_map = tone_map
        self.log = log or (lambda message: None)
        # 无限源（server/sources.py）；None 表示 url 是本地文件。
        self.input_source = source
        self.video_ended = threading.Event()
        self.audio_ended = threading.Event()
        self.stop = threading.Event()
        # 读取线程放入数据时置位，发送端空闲时据此提前醒来。
        self.wake = threading.Event()
        self.lock = threading.Lock()
        # 会话携带的轨道；probe() 确定，之后不变。
        self.tracks: Tracks | None = None
        self.audio: collections.deque[bytes] = collections.deque(maxlen=AUDIO_QUEUE_CHUNKS)
        # (到达时刻, 原始帧)；容量在 start() 中按帧率与延后时长确定。
        self.video: collections.deque[tuple[float, bytes]] = collections.deque()
        self.fps = FPS
        # 画面相对到达的延后秒数；带音频轨才延后，见 AV_LEAD_MS。
        self.lead_s = 0.0
        # 文件源不丢内容：队列满时阻塞读取，起播突发的帧按帧率排开；无限源（桌面捕捉）丢旧帧保低延迟。
        self.lossless = source is None
        self._video_stamp = 0.0
        self.last_media = time.monotonic()
        # 音频发送时钟：首块发出的时刻与已发块数，见 pop_audio。
        self._audio_anchor: float | None = None
        self._audio_popped = 0
        self.error: Exception | None = None
        self.dropped_video = 0
        self.dropped_audio = 0
        self.decoder: subprocess.Popen | None = None
        self.threads: list[threading.Thread] = []
        self._tail: collections.deque[bytes] = collections.deque(maxlen=32)
        self._video: object | None = None
        self._audio: object | None = None
        self._socks: list[socket.socket] = []
        self._adpcm = adpcm.Encoder()
        # 已发给设备的画面与强制刷新条带的轮转计数（见 frames.choose_stripes）；仅发送线程使用。
        self._shown: bytes | None = None
        self._delta_tick = 0
        self._snap_level = 0
        self._frame_target = start_rate(FPS) // FPS
        self.palette: bytes | None = None

    def build_palette(self) -> bytes:
        """固定 3-3-2 调色板，不按源取样。"""
        self.palette = default_palette()
        return self.palette

    def _accept_decoder(self, listener: socket.socket) -> socket.socket:
        """接受解码器的连接；解码器已退出（如源打不开）则立即失败，不等满超时。"""
        deadline = time.monotonic() + DECODER_CONNECT_TIMEOUT_S
        listener.settimeout(0.1)
        while True:
            try:
                return listener.accept()[0]
            except socket.timeout:
                pass
            if self.decoder is not None and self.decoder.poll() is not None:
                raise ConnectionAbortedError("decoder exited")
            if time.monotonic() > deadline:
                raise TimeoutError("decoder connect timeout")

    def _resolve_fps(self) -> None:
        """在产出任何内容前确定一次画面帧率；显式设置 `TV_FPS` 时直接采用。"""
        if self.input_source is not None:
            self.fps = self.input_source.fps
        elif "TV_FPS" in os.environ:
            self.fps = FPS
        else:
            self.fps = probe_source_fps(self.url, self.ffmpeg) or FPS
        self._frame_target = start_rate(self.fps) // self.fps

    def probe(self) -> Tracks:
        """确定会话携带的轨道；须在发 CONFIG 之前调用，重复调用返回同一结果。"""
        if self.tracks is None:
            if self.input_source is not None:
                self.tracks = self.input_source.tracks
            else:
                has_audio, _ = probe_audio(self.url, self.ffmpeg)
                self.tracks = Tracks(audio=has_audio, video=True)
        return self.tracks

    def start(self) -> None:
        if self.palette is None:
            raise LiveError("start() before build_palette()")
        tracks = self.probe()
        self._resolve_fps()
        self.gpu = self.hwaccel_mode == "qsv"
        self.lead_s = AV_LEAD_MS / 1000 if tracks.audio else 0.0
        self.video = collections.deque(
            maxlen=int((self.lead_s + VIDEO_QUEUE_SLACK_S) * self.fps) + 2)

        # 父进程先监听端口再启动 ffmpeg，由 ffmpeg 作为客户端回连；有音轨的文件有画面与音频两个端口，
        # 其余只有画面。失败时须关闭全部资源，否则设备反复重连会耗尽套接字。
        file_audio = self.input_source is None and tracks.audio
        listeners: list[socket.socket] = []
        accepted: list[socket.socket] = []
        success = False
        try:
            for _ in range(2 if file_audio else 1):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listeners.append(listener)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
            ports = [listener.getsockname()[1] for listener in listeners]

            self.decoder = subprocess.Popen(
                source_command(self.url, ports[0], ports[1] if len(ports) > 1 else None,
                               self.ffmpeg, fps=self.fps, source=self.input_source, gpu=self.gpu,
                               tone_map=self.tone_map),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            # stderr 须持续读取：管道无人读时 ffmpeg 会阻塞，与源停供无法区分。
            self._spawn(self._drain_stderr, "live-decoder-log")

            try:
                for listener in listeners:
                    accepted.append(self._accept_decoder(listener))
            except OSError as error:
                raise LiveError(f"decoder did not connect: {type(error).__name__}") from None
            for listener in listeners:
                listener.close()
            listeners = []

            # 关闭 Nagle 算法：小写入最多被攒 40 ms，恰为一个音频块长。
            for sock in accepted:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            # 加大视频接收缓冲（尽力而为）：容下一帧原始画面。
            try:
                accepted[0].setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
            except OSError:
                pass

            self._socks = list(accepted)
            self._video = accepted[0].makefile("rb", buffering=0)
            self._spawn(self._read_video, "live-video")
            if file_audio:
                self._audio = accepted[1].makefile("rb", buffering=0)
                self._spawn(self._read_audio, "live-audio")
            elif tracks.audio:
                # 无限源的声音在本进程产生，不经过 ffmpeg。
                loopback.Loopback(self._push_pcm, self.stop, self.log).start()
            success = True
        finally:
            for sock in listeners:
                try:
                    sock.close()
                except OSError:
                    pass
            if not success:
                for sock in accepted:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self.close()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, daemon=True, name=name)
        thread.start()
        self.threads.append(thread)

    def _note(self, error: Exception) -> None:
        with self.lock:
            if self.error is None:
                self.error = error
        self.stop.set()

    def _drain_stderr(self) -> None:
        try:
            for line in iter(self.decoder.stderr.readline, b""):
                self._tail.append(line)
        except (OSError, ValueError):
            pass

    def _decoder_finished(self) -> bool:
        """解码器是否已正常退出（源读完）；两路管道关闭后进程随即结束。"""
        try:
            return self.decoder is not None and self.decoder.wait(timeout=5.0) == 0
        except subprocess.TimeoutExpired:
            return False

    def _stream_ended(self, ended: threading.Event) -> None:
        """一路管道读到结尾：解码器正常退出才算源读完，否则是故障。"""
        if self.stop.is_set():
            return
        if self._decoder_finished():
            ended.set()
            self.wake.set()
        else:
            self._note(LiveError("transcode pipe closed"))

    def _read_video(self) -> None:
        """立即把 ffmpeg 画面输出读进队列；读取一旦停顿，ffmpeg 的写入阻塞，整个转码随之冻结。"""
        try:
            while not self.stop.is_set():
                frame = frames.read_frame(self._video, self.stop)
                if frame is None:
                    self._stream_ended(self.video_ended)
                    return
                now = time.monotonic()
                if self.lossless:
                    while len(self.video) >= self.video.maxlen and not self.stop.is_set():
                        self.stop.wait(0.005)
                    # ffmpeg 起播时一次吐出约 0.5 秒的帧；到达时刻顺延到间隔不小于一帧，使它们逐帧发出。
                    now = max(time.monotonic(), self._video_stamp + 1.0 / self.fps)
                    self._video_stamp = now
                with self.lock:
                    if len(self.video) == self.video.maxlen:
                        self.dropped_video += 1
                    self.video.append((now, frame))
                    self.last_media = now
                self.wake.set()
        except (OSError, ValueError) as error:
            self._note(error)

    def _read_audio(self) -> None:
        """立即把 ffmpeg 音频输出读进队列；读取一旦停顿，ffmpeg 的写入阻塞，整个转码随之冻结。"""
        try:
            while not self.stop.is_set():
                block = frames.read_exactly(self._audio, AUDIO_PCM_BYTES)
                if len(block) != AUDIO_PCM_BYTES:
                    self._stream_ended(self.audio_ended)
                    return
                while len(self.audio) >= self.audio.maxlen and not self.stop.is_set():
                    self.stop.wait(0.005)
                self._push_pcm(block)
        except (OSError, ValueError) as error:
            self._note(error)

    def _push_pcm(self, block: bytes) -> None:
        """编码一个 40 ms 的 PCM 块并入队；每块自带解码状态，丢弃任一块不影响其余。"""
        data = self._adpcm.encode_pcm(block)
        with self.lock:
            if len(self.audio) == self.audio.maxlen:
                self.dropped_audio += 1
            self.audio.append(data)
            self.last_media = time.monotonic()
        self.wake.set()

    def pop_audio(self) -> bytes | None:
        """取最早的音频块；没有，或发送会领先实时超过 AUDIO_AHEAD_MS 时返回 None。"""
        with self.lock:
            if not self.audio:
                return None
            now = time.monotonic()
            if self._audio_anchor is None:
                self._audio_anchor = now
            due = (self._audio_anchor + self._audio_popped * AUDIO_CHUNK_MS / 1000
                   - AUDIO_AHEAD_MS / 1000)
            if now < due:
                return None
            self._audio_popped += 1
            return self.audio.popleft()

    def pop_video(self) -> list[bytes] | None:
        """取最早一个已到发送时刻的画面帧并编码成包，没有到期的帧返回 None。

        发送循环偶尔晚醒会使相邻两帧同时到期，逐帧发出不丢帧；到期积压超过 `MAX_DUE_FRAMES`
        说明发送端持续落后，丢最旧的追上最新画面。编码依赖设备已显示的画面，故只在确定要写出时调用。
        """
        cutoff = time.monotonic() - self.lead_s
        with self.lock:
            due = 0
            for arrived, _ in self.video:
                if arrived > cutoff:
                    break
                due += 1
            if not due:
                return None
            for _ in range(0 if self.lossless else max(0, due - MAX_DUE_FRAMES)):
                self.video.popleft()
                self.dropped_video += 1
            raw = self.video.popleft()[1]
        return self._encode(raw)

    def _encode(self, raw: bytes) -> list[bytes]:
        """把原始帧转成线上包，并记下设备此后显示的画面。"""
        if not frames.DELTA:
            return frames.frame_packets(raw)
        target = self._frame_target
        if DEGRADE in perceptual.MODES:
            chosen, drawn, rung = perceptual.encode_within(
                raw, self._shown, self._delta_tick, target, self._snap_level, mode=DEGRADE)
            self._snap_level = rung
        else:
            chosen, drawn, _ = frames.encode_within(raw, self._shown, self._delta_tick, target)
        if ENCODE == "fill":
            chosen = frames.fill_stripes(drawn, self._shown, chosen, target)
        self._delta_tick += 1
        self._shown = frames.apply_stripes(drawn, self._shown, chosen)
        return frames.pack_stripes(chosen)

    def finished(self) -> bool:
        """文件源已读完，且各轨队列都已取空；无限源不会读完。没有音频轨时只看画面。"""
        with self.lock:
            if self.input_source is not None or not self.video_ended.is_set() or self.video:
                return False
            return not self.tracks.audio or (self.audio_ended.is_set() and not self.audio)

    def stalled(self, seconds: float) -> bool:
        """超过 `seconds` 没有任何新内容入队。"""
        return time.monotonic() - self.last_media > seconds

    def failure(self) -> Exception | None:
        with self.lock:
            return self.error

    @staticmethod
    def _sanitize_diagnostics(text: str) -> str:
        """去除诊断文本中的令牌、密码与敏感参数。"""
        text = re.sub(r"([?&][a-zA-Z0-9_.-]+=)[^\s&'\"<>]+", r"\1<redacted>", text)
        return re.sub(r"(https?://)([^:@\s/]+:[^:@\s/]+@)", r"\1<auth>@", text)

    def diagnostics(self) -> str:
        """转码错误文本，用于日志：只含 ffmpeg stderr，不含媒体内容。"""
        parts = []
        error = self.failure()
        if error is not None:
            parts.append(type(error).__name__)
        if self.decoder is not None and self.decoder.poll() is not None:
            parts.append(f"decoder_exit={self.decoder.returncode}")
        lines = [line.decode("utf-8", errors="replace").strip() for line in self._tail]
        text = " ".join(line for line in lines if line)
        if text:
            parts.append(f"decoder: {self._sanitize_diagnostics(text)[-300:]}")
        return " | ".join(parts)

    def close(self) -> None:
        self.stop.set()
        # 先关文件对象，使读线程见到 EOF，再显式关套接字：只关文件对象不会释放 fd。
        for stream in (self._video, self._audio):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._video = self._audio = None
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self._socks = []
        proc = self.decoder
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except OSError:
                    pass
            except OSError:
                pass
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=1.0)
        if proc is not None and proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass
