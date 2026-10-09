import re
import struct

MAX_PAYLOAD = 512


def validate_number(number: str) -> str:
    if not isinstance(number, str) or not re.fullmatch(r"\+?[0-9]{3,30}", number):
        raise ValueError("号码只接受 3–30 位数字及开头的 +，请提供完整号码")
    return number


def encode_frame(direction: int, sequence: int, payload: bytes) -> bytes:
    if direction not in range(4) or not 0 < len(payload) <= MAX_PAYLOAD:
        raise ValueError("Invalid frame")
    raw = struct.pack("<BHH", direction, sequence & 65535, len(payload)) + payload
    result = bytearray([0])
    index, code = 0, 1
    for byte in raw:
        if byte == 0:
            result[index] = code
            index = len(result)
            result.append(0)
            code = 1
        else:
            result.append(byte)
            code += 1
            if code == 255:
                result[index] = code
                index = len(result)
                result.append(0)
                code = 1
    result[index] = code
    result.append(0)
    return bytes(result)


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.dropping = False
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        frames = []
        for byte in data:
            if byte:
                if not self.dropping:
                    self.buffer.append(byte)
                    if len(self.buffer) > 550:
                        self.buffer.clear()
                        self.dropping = True
                        self.errors += 1
                continue
            if self.buffer and not self.dropping:
                try:
                    frames.append(self.decode(bytes(self.buffer)))
                except ValueError:
                    self.errors += 1
            self.buffer.clear()
            self.dropping = False
        return frames

    @staticmethod
    def decode(encoded: bytes) -> tuple[int, int, bytes]:
        raw = bytearray()
        index = 0
        while index < len(encoded):
            code = encoded[index]
            index += 1
            end = index + code - 1
            if code == 0 or end > len(encoded):
                raise ValueError("Malformed COBS frame")
            raw.extend(encoded[index:end])
            index = end
            if code < 255 and index < len(encoded):
                raw.append(0)
        if len(raw) < 5:
            raise ValueError("Short header")
        direction, sequence, length = struct.unpack("<BHH", raw[:5])
        if direction > 3 or not 0 < length <= MAX_PAYLOAD or len(raw) != length + 5:
            raise ValueError("Invalid length or direction")
        return direction, sequence, bytes(raw[5:])
