import json
import wave
from pathlib import Path


class CallRecording:
    """Record received/sent PCM against one monotonic call timeline."""

    def __init__(self, path: Path, rate: int):
        path.mkdir(parents=True, exist_ok=True)
        self.rate = rate
        self.files = {}
        self.positions = {"remote": 0, "agent": 0}
        for name in self.positions:
            handle = wave.open(str(path / f"{name}.wav"), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            self.files[name] = handle
        self.transcripts = (path / "transcripts.jsonl").open("a", encoding="utf-8")

    def write(self, track: str, pcm: bytes, timestamp: float):
        if len(pcm) % 2:
            raise ValueError("Unaligned PCM16")
        target = max(0, round(timestamp * self.rate))
        gap = max(0, target - self.positions[track])
        self._silence(track, gap)
        self.files[track].writeframesraw(pcm)
        self.positions[track] += len(pcm) // 2

    def _silence(self, track: str, samples: int):
        while samples:
            count = min(samples, self.rate)
            self.files[track].writeframesraw(bytes(count * 2))
            self.positions[track] += count
            samples -= count

    def transcript(self, role: str, text: str, timestamp: float, **details):
        self.transcripts.write(json.dumps({"role": role, "text": text, "seconds": timestamp,
                                          **details}, ensure_ascii=False) + "\n")
        self.transcripts.flush()

    def close(self, duration: float):
        for track, handle in self.files.items():
            self._silence(track, max(0, round(duration * self.rate) - self.positions[track]))
            handle.close()
        self.transcripts.close()
