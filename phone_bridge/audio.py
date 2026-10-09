from collections import deque

import numpy as np
import soxr


class Resampler:
    def __init__(self, source: int, target: int):
        self.stream = soxr.ResampleStream(source, target, 1, dtype="int16", quality="LQ")

    def convert(self, pcm: bytes, final=False) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM16 alignment")
        return self.stream.resample_chunk(np.frombuffer(pcm, dtype="<i2"), last=final).astype("<i2").tobytes()


class Playback:
    def __init__(self, rate: int):
        self.rate = rate
        self.pending = deque()
        self.blocked = set()
        self.sent = {}
        self.last_item = None
        self.latest_item = None
        self.completed = set()
        self.bytes_queued = 0

    def append(self, item: str, pcm: bytes):
        if item in self.blocked:
            return
        self.latest_item = item
        if not pcm:
            return
        if self.bytes_queued + len(pcm) > self.rate * 2 * 30:
            raise RuntimeError("模型输出积压超过 30 秒")
        self.pending.append((item, pcm))
        self.bytes_queued += len(pcm)

    def pop(self, size: int):
        if not self.pending:
            return None
        item = self.pending[0][0]
        available = 0
        for queued_item, pcm in self.pending:
            if queued_item != item:
                break
            available += len(pcm)
            if available >= size:
                break
        if available < size and item not in self.completed:
            return None
        # Network deltas are arbitrary chunks; only phone packets have a fixed cadence.
        pieces = []
        remaining = min(size, available)
        actual = remaining
        while remaining:
            queued_item, pcm = self.pending.popleft()
            count = min(remaining, len(pcm))
            pieces.append(pcm[:count])
            remaining -= count
            if count < len(pcm):
                self.pending.appendleft((queued_item, pcm[count:]))
        chunk = b"".join(pieces)
        self.bytes_queued -= actual
        self.sent[item] = self.sent.get(item, 0) + actual
        self.last_item = item
        # Complete the final phone frame without leaving an unplayable SCO fragment.
        return item, chunk + bytes(size - actual)

    def interrupt(self):
        item = self.latest_item
        if not self.pending and item in self.completed:
            return None, 0
        self.blocked.update(i for i, _ in self.pending)
        if item:
            self.blocked.add(item)
        self.pending.clear()
        self.bytes_queued = 0
        return item, (self.sent.get(item, 0) * 1000 // (self.rate * 2))

    def finish(self, item: str):
        self.completed.add(item)
