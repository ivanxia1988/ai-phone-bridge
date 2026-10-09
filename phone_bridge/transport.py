import threading
import time
from collections.abc import Callable

import serial
from serial.tools import list_ports

from .protocol import FrameDecoder, encode_frame


def detect_port() -> str:
    ports = [p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x10C4, 0xEA60)]
    preferred = [p for p in ports if "SLAB_USBtoUART" in p]
    if len(preferred) == 1:
        return preferred[0]
    if len(ports) == 1:
        return ports[0]
    raise RuntimeError("未找到唯一 CP2102 串口，请设置 ESP32_PORT")


class SerialTransport:
    def __init__(self, port: str, callback: Callable[[int, bytes], None], baud=921600):
        self.decoder = FrameDecoder()
        self.sequence = 0
        self.callback = callback
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.error = None
        self.port = serial.Serial(port=None, baudrate=baud, timeout=0.03, write_timeout=1)
        self.port.dtr = False
        self.port.rts = False
        self.port.port = port
        self.port.open()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                data = self.port.read(max(1, min(self.port.in_waiting, 4096)))
                for direction, _, payload in self.decoder.feed(data):
                    self.callback(direction, payload)
        except Exception as exc:
            if not self.closed.is_set():
                self.error = type(exc).__name__
                self.callback(2, b"evt:transport_lost")

    def send(self, direction: int, payload: bytes):
        if self.error or self.closed.is_set():
            raise RuntimeError("ESP32 串口连接已中断")
        with self.lock:
            frame = encode_frame(direction, self.sequence, payload)
            self.sequence = (self.sequence + 1) & 65535
            written = self.port.write(frame)
            if written != len(frame):
                raise RuntimeError("串口未完整写入")

    def control(self, text: str):
        self.send(2, text.encode("ascii"))

    def close(self):
        self.closed.set()
        self.thread.join(timeout=1)
        self.port.close()


def probe(port: str, baud=921600, count=120):
    condition = threading.Condition()
    messages = []

    def received(direction, payload):
        with condition:
            messages.append((direction, payload))
            condition.notify_all()

    transport = SerialTransport(port, received, baud)
    try:
        # CP2102 drivers may reset the board on open despite preset DTR/RTS.
        time.sleep(1.5)
        # A delimiter discards any ROM boot text that precedes the app protocol.
        transport.port.write(b"\x00")
        deadline = time.monotonic() + 8
        state = None
        while time.monotonic() < deadline and state is None:
            transport.control("query_call")
            with condition:
                condition.wait(0.4)
                state = next((p.decode("ascii") for d, p in messages if d == 2 and p.startswith(b"state:")), None)
        if state is None or "firmware=1" not in state:
            raise RuntimeError("串口存在，但未收到本项目固件响应")
        messages.clear()
        for index in range(count):
            payload = index.to_bytes(2, "little") + bytes(range(256)) + bytes(254)
            transport.send(3, payload)
            deadline = time.monotonic() + 1
            with condition:
                while (3, payload) not in messages and time.monotonic() < deadline:
                    condition.wait(0.05)
                if (3, payload) not in messages:
                    raise RuntimeError(f"二进制回环失败：第 {index + 1} 帧")
                messages.remove((3, payload))
        return {"port": port, "baud": baud, "state": state, "echo_frames": count,
                "echo_bytes": count * 512, "decode_errors": transport.decoder.errors}
    finally:
        transport.close()
