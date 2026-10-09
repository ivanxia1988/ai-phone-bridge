#!/usr/bin/env python3
import argparse
import queue
import struct
import sys
import time

import numpy as np
import serial


# Hardcoded uplink format expected by ESP bridge.
SAMPLE_RATE = 16000
CHUNK_BYTES = 240  # 120 samples @16k mono PCM16
CHUNK_SAMPLES = CHUNK_BYTES // 2
CAPTURE_QUEUE_MAX_BLOCKS = 256


def cobs_encode(data: bytes) -> bytes:
    if not data:
        return b"\x01"

    out = bytearray([0])
    code_index = 0
    code = 1

    for b in data:
        if b == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1

    out[code_index] = code
    return bytes(out)


def build_packet(direction: int, seq: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    return struct.pack("<BHH", direction, seq & 0xFFFF, len(payload)) + payload


def pick_loopback_device(sd, user_device):
    if user_device is not None:
        return user_device

    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    # Prefer default WASAPI output's matching loopback input.
    default_out = -1
    try:
        default_out = sd.default.device[1]
    except Exception:
        default_out = -1

    if default_out is not None and default_out >= 0 and default_out < len(devices):
        out_dev = devices[default_out]
        hostapi_index = out_dev.get("hostapi", -1)
        hostapi_name = ""
        if 0 <= hostapi_index < len(hostapis):
            hostapi_name = str(hostapis[hostapi_index].get("name", "")).lower()
        base_name = str(out_dev.get("name", "")).strip()

        # On many setups, PortAudio exposes "... (loopback)" as an input device.
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if dev.get("hostapi", -1) != hostapi_index:
                continue
            name = str(dev.get("name", ""))
            if base_name and base_name in name and "loopback" in name.lower():
                return idx

        # Some drivers expose output device itself as capturable input.
        if "wasapi" in hostapi_name and out_dev.get("max_input_channels", 0) > 0:
            return default_out

    # Fallback: first WASAPI loopback-style input.
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        hostapi_index = dev.get("hostapi", -1)
        hostapi_name = ""
        if 0 <= hostapi_index < len(hostapis):
            hostapi_name = str(hostapis[hostapi_index].get("name", "")).lower()
        name = str(dev.get("name", "")).lower()
        if "wasapi" in hostapi_name and "loopback" in name:
            return idx

    # Last resort: any input device.
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            return idx
    raise RuntimeError("no loopback-capable input device found")


class SoxrStreamResampler:
    """Stateful soxr-based mono PCM16 stream resampler."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._stream = None
        self._stream_call = None
        self._stream_call_name = ""

        if self.src_rate == self.dst_rate:
            return

        try:
            import soxr  # type: ignore
        except Exception as e:
            raise RuntimeError("missing dependency: pip install soxr") from e

        try:
            self._stream = soxr.ResampleStream(self.src_rate, self.dst_rate, 1, dtype="float32", quality="HQ")
        except TypeError:
            # Older python-soxr may expose fewer kwargs.
            self._stream = soxr.ResampleStream(self.src_rate, self.dst_rate, 1)

        for name in ("resample_chunk", "process", "resample"):
            fn = getattr(self._stream, name, None)
            if callable(fn):
                self._stream_call = fn
                self._stream_call_name = name
                break
        if self._stream_call is None:
            raise RuntimeError("unsupported soxr ResampleStream API")

    def process(self, samples_i16: np.ndarray) -> np.ndarray:
        if samples_i16.size == 0:
            return np.empty((0,), dtype=np.int16)
        if self._stream is None:
            return samples_i16.astype(np.int16, copy=False)

        xin = samples_i16.astype(np.float32) / 32768.0
        if self._stream_call_name == "resample_chunk":
            try:
                y = self._stream_call(xin, last=False)
            except TypeError:
                y = self._stream_call(xin)
        else:
            y = self._stream_call(xin)

        if y is None:
            return np.empty((0,), dtype=np.int16)
        y = np.asarray(y, dtype=np.float32)
        if y.size == 0:
            return np.empty((0,), dtype=np.int16)
        y = np.clip(y, -1.0, 1.0)
        return (y * 32767.0).astype(np.int16)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture speaker(loopback) audio and inject to ESP32 uplink (UART + COBS + dir=1)."
    )
    ap.add_argument("--port", default="COM17", help="Serial port")
    ap.add_argument("--baud", type=int, default=1500000, help="Baud rate")
    ap.add_argument("--device", default=None, help="Output device index/name for loopback capture")
    args = ap.parse_args()

    try:
        import sounddevice as sd  # type: ignore
    except Exception:
        print("missing dependency: pip install sounddevice", file=sys.stderr)
        return 1

    try:
        loopback_dev = pick_loopback_device(sd, args.device)
        dev_info = sd.query_devices(loopback_dev)
    except Exception as e:
        print(f"failed to pick loopback device: {e}", file=sys.stderr)
        return 1

    cap_rate = int(dev_info.get("default_samplerate", 48000) or 48000)
    channels = 2 if int(dev_info.get("max_input_channels", 0)) >= 2 else 1
    if channels < 1:
        print("selected device has no input channels for capture", file=sys.stderr)
        return 1

    chunk_sec = CHUNK_SAMPLES / float(SAMPLE_RATE)
    cap_block_frames = max(1, int(round(cap_rate * chunk_sec)))

    print(f"Open serial {args.port} @ {args.baud}")
    print(
        f"Loopback device={loopback_dev} name={dev_info.get('name')} "
        f"capture_rate={cap_rate}Hz ch={channels} -> uplink_rate={SAMPLE_RATE}Hz mono"
    )

    seq = 0
    sent_bytes = 0
    sent_packets = 0
    dropped_packets = 0
    dropped_capture_blocks = 0
    start = time.monotonic()
    pending = bytearray()
    try:
        resampler = SoxrStreamResampler(cap_rate, SAMPLE_RATE)
    except Exception as e:
        print(f"failed to init soxr resampler: {e}", file=sys.stderr)
        return 1
    next_tx_t = start
    cap_q = queue.Queue(maxsize=CAPTURE_QUEUE_MAX_BLOCKS)

    def on_audio(indata, frames, time_info, status):
        nonlocal dropped_capture_blocks
        _ = frames
        _ = time_info
        _ = status
        try:
            cap_q.put_nowait(bytes(indata))
        except queue.Full:
            dropped_capture_blocks += 1

    try:
        with serial.Serial(args.port, args.baud, timeout=0, write_timeout=0.05) as ser:
            with sd.RawInputStream(
                samplerate=cap_rate,
                blocksize=cap_block_frames,
                device=loopback_dev,
                channels=channels,
                dtype="int16",
                callback=on_audio,
            ) as in_stream:
                while True:
                    _ = in_stream
                    try:
                        raw = cap_q.get(timeout=1.0)
                    except queue.Empty:
                        continue

                    arr = np.frombuffer(raw, dtype=np.int16)
                    if channels > 1:
                        usable = (arr.size // channels) * channels
                        if usable != arr.size:
                            arr = arr[:usable]
                    if channels > 1:
                        arr = arr.reshape(-1, channels).mean(axis=1).astype(np.int16)
                    mono_16k = resampler.process(arr)
                    pending.extend(mono_16k.tobytes())

                    while len(pending) >= CHUNK_BYTES:
                        now = time.monotonic()
                        if now < next_tx_t:
                            time.sleep(next_tx_t - now)
                        elif now - next_tx_t > 0.2:
                            # If we are too far behind, realign pacing to now.
                            next_tx_t = now

                        chunk = bytes(pending[:CHUNK_BYTES])
                        del pending[:CHUNK_BYTES]

                        seq = (seq + 1) & 0xFFFF
                        pkt = build_packet(1, seq, chunk)
                        frame = cobs_encode(pkt) + b"\x00"
                        try:
                            n = ser.write(frame)
                            if n != len(frame):
                                dropped_packets += 1
                                next_tx_t += chunk_sec
                                continue
                        except serial.SerialTimeoutException:
                            dropped_packets += 1
                            next_tx_t += chunk_sec
                            continue

                        sent_packets += 1
                        sent_bytes += len(chunk)
                        next_tx_t += chunk_sec

                        if (sent_packets % 200) == 0:
                            elapsed = max(time.monotonic() - start, 1e-6)
                            kbps = sent_bytes * 8.0 / elapsed / 1000.0
                            print(
                                f"sent={sent_packets} audio_bytes={sent_bytes} "
                                f"uart_drop={dropped_packets} cap_drop={dropped_capture_blocks} "
                                f"rate={kbps:.1f}kbps"
                            )
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"runtime error: {e}", file=sys.stderr)
        return 1

    total = max(time.monotonic() - start, 1e-6)
    kbps = sent_bytes * 8.0 / total / 1000.0
    print(
        f"Done: packets={sent_packets}, audio_bytes={sent_bytes}, "
        f"uart_drop={dropped_packets}, cap_drop={dropped_capture_blocks}, avg_rate={kbps:.1f} kbps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
