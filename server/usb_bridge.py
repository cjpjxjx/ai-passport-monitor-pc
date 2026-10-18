"""在本机 TCP 服务器与设备的 USB 串口之间转发 FAV1 流。

服务器照常监听本机端口，桥接扮演设备：设备经串口发来 HELLO 时去连该端口，把服务器发出的
字节写进串口，把设备的 HELLO 与 END 转回服务器。编码、节奏与协议都不在这里实现，设备收到
的与经 Wi-Fi 收到的一致。

不把串口交给服务器：服务器用 select 等待连接，Windows 上 select 只接受 socket。

背压保持真实：桥接写满线缆才继续读服务器的 socket，线缆慢时服务器的发送缓冲填满，
和慢网络一样表现为“不可写”。

设备的 USB 驱动没有流控，接收环满时到达的字节会丢，所以每次写入不超过 CHUNK 字节。
"""

from __future__ import annotations

import socket
import threading
import time

import serial

MAGIC = b"FAV1"
HEADER = 24
KIND_HELLO, KIND_END = 1, 5
CHUNK = 2048
BAUDRATE = 921600
# 设备日志中值得显示的行；其余日志行丢弃。
WATCH = ("AUDIO_EMPTY", "Session reset", "RX_EXIT", "RX_IO", "rejected", "nobuf")
# 服务器结束后等待最后的字节（END）写进线缆的时限（秒）。
DRAIN_S = 5.0


class UsbBridge:
    def __init__(self, port: str, server, logger=None):
        """打开串口；server 是 AVServer，桥接随它的 stop 结束，串口丢失时反过来停止它。"""
        self.server = server
        self.logger = logger or (lambda message: None)
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = BAUDRATE
        self.ser.timeout = 0.0
        self.ser.write_timeout = 3.0
        # 打开前先置低：板子的自动复位电路接在这两根线上。
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.done = threading.Event()
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def device_line(self, line: str) -> None:
        if any(word in line for word in WATCH):
            self.logger(f"device: {line[:170]}")

    def connect_server(self) -> None:
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.settimeout(0.2)
            self.sock = sock

    def lost(self, message: str) -> None:
        self.logger(message)
        self.done.set()
        self.server.stop.set()

    # 设备到服务器：串口里混着日志文本和 FAV1 包，靠魔数分开。
    def device_reader(self) -> None:
        buf = bytearray()
        while not self.done.is_set():
            try:
                chunk = self.ser.read(4096)
            except (serial.SerialException, OSError):
                self.lost("serial port lost")
                return
            if not chunk:
                time.sleep(0.002)
                continue
            buf.extend(chunk)
            while True:
                at = buf.find(MAGIC)
                if at < 0:
                    if len(buf) > 3:
                        self.text(bytes(buf[:-3]))
                        del buf[:-3]
                    break
                if at:
                    self.text(bytes(buf[:at]))
                    del buf[:at]
                if len(buf) < HEADER:
                    break
                kind = buf[5]
                length = int.from_bytes(buf[20:24], "big")
                if buf[4] != 1 or kind not in (KIND_HELLO, KIND_END) or length > 4096:
                    self.text(bytes(buf[:1]))
                    del buf[:1]
                    continue
                if len(buf) < HEADER + length:
                    break
                packet = bytes(buf[:HEADER + length])
                del buf[:HEADER + length]
                self.forward_up(kind, packet)

    def text(self, raw: bytes) -> None:
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                self.device_line(line.strip())

    def forward_up(self, kind: int, packet: bytes) -> None:
        if kind == KIND_HELLO:
            self.logger("device HELLO: opening a session with the server")
            try:
                self.connect_server()
            except OSError as error:
                self.logger(f"cannot reach the server: {type(error).__name__}")
                return
        with self.lock:
            sock = self.sock
        if sock:
            try:
                sock.sendall(packet)
            except OSError as error:
                self.logger(f"forward to server failed: {type(error).__name__}")

    # 服务器到设备。
    def server_reader(self) -> None:
        while not self.done.is_set():
            with self.lock:
                sock = self.sock
            if not sock:
                time.sleep(0.01)
                continue
            try:
                data = sock.recv(CHUNK)
            except socket.timeout:
                continue
            except OSError:
                data = b""
            if not data:
                with self.lock:
                    if self.sock is sock:
                        self.sock = None
                self.logger("server closed the session")
                continue
            try:
                self.ser.write(data)
            except (serial.SerialException, OSError) as error:
                self.lost(f"write to device failed: {type(error).__name__}")
                return

    def run(self) -> None:
        """服务器就绪后开始转发，服务器结束且最后的字节写完后返回并关闭串口。"""
        while not self.server.ready.is_set() and not self.server.stop.is_set():
            time.sleep(0.05)
        threads = [threading.Thread(target=self.device_reader, daemon=True),
                   threading.Thread(target=self.server_reader, daemon=True)]
        for thread in threads:
            thread.start()
        # 上次运行遗留的会话在等包头且保持静默；不是包头的 24 字节会让它立刻结束。
        self.logger("waiting for the device on USB")
        time.sleep(1.0)
        try:
            with self.lock:
                idle = self.sock is None
            if idle:
                self.ser.write(bytes(HEADER))
            while not self.server.stop.is_set() and not self.done.is_set():
                time.sleep(0.1)
            deadline = time.monotonic() + DRAIN_S
            while self.sock is not None and time.monotonic() < deadline and not self.done.is_set():
                time.sleep(0.05)
        except (serial.SerialException, OSError) as error:
            self.logger(f"usb bridge error: {type(error).__name__}")
        finally:
            self.done.set()
            try:
                self.ser.write(bytes(HEADER))
            except (serial.SerialException, OSError):
                pass
            try:
                self.ser.close()
            except (serial.SerialException, OSError):
                pass
