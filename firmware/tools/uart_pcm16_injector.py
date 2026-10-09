#!/usr/bin/env python3
import argparse
import math
import struct
import sys
import time

import serial


# Hardcoded audio settings.
SAMPLE_RATE = 16000
CHUNK_BYTES = 240
TONE_START_HZ = 1000.0
TONE_END_HZ = 500.0
TONE_DURATION_SEC = 1.0
TONE_AMP_START = 0.85
TONE_AMP_END = 0.10


def try_import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore

        return sd
    except Exception:
        return None


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


def chunk_iter(data: bytes, chunk_bytes: int):
    for i in range(0, len(data), chunk_bytes):
        yield data[i : i + chunk_bytes]


def generate_sweep_pcm16() -> bytes:
    total_samples = int(SAMPLE_RATE * TONE_DURATION_SEC)
    t_total = total_samples / float(SAMPLE_RATE)
    k = (TONE_END_HZ - TONE_START_HZ) / t_total

    pcm = bytearray(total_samples * 2)
    two_pi = 2.0 * math.pi
    offset = 0

    for i in range(total_samples):
        t = i / float(SAMPLE_RATE)
        # Log envelope (linear in log-domain): perceptually smoother decay.
        x = t / t_total
        amp = TONE_AMP_START * ((TONE_AMP_END / TONE_AMP_START) ** x)
        phase = two_pi * (TONE_START_HZ * t + 0.5 * k * t * t)
        sample = int(32767.0 * amp * math.sin(phase))
        struct.pack_into("<h", pcm, offset, sample)
        offset += 2

    return bytes(pcm)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inject generated PCM16 sweep into ESP32 bridge uplink (UART + COBS + dir=1)."
    )
    ap.add_argument("--port", default="COM17", help="Serial port")
    ap.add_argument("--baud", type=int, default=1500000, help="Baud rate")
    ap.add_argument("--play", action="store_true", help="Play generated audio locally while sending")
    args = ap.parse_args()

    pcm = generate_sweep_pcm16()
    secs = len(pcm) / 2.0 / SAMPLE_RATE

    print(f"Open serial {args.port} @ {args.baud}")
    print(
        "Tone: "
        f"{TONE_START_HZ:.1f}Hz->{TONE_END_HZ:.1f}Hz, "
        f"duration={TONE_DURATION_SEC:.3f}s, amp={TONE_AMP_START:.2f}->{TONE_AMP_END:.2f}, "
        f"sample_rate={SAMPLE_RATE}, chunk={CHUNK_BYTES}, loop=True"
    )
    print(f"Buffer size: {len(pcm)} bytes (~{secs:.3f}s)")
    if args.play:
        print("Local playback: enabled")

    seq = 0
    sent_bytes = 0
    sent_packets = 0
    start = time.monotonic()

    sd = None
    out_stream = None
    if args.play:
        sd = try_import_sounddevice()
        if sd is None:
            print("playback requested but sounddevice is missing: pip install sounddevice", file=sys.stderr)
            return 1
        try:
            out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=CHUNK_BYTES // 2,
                latency="low",
            )
            out_stream.start()
        except Exception as e:
            print(f"failed to open speaker output: {e}", file=sys.stderr)
            return 1

    try:
        with serial.Serial(args.port, args.baud, timeout=0, write_timeout=1) as ser:
            while True:
                loop_start = time.monotonic()
                for chunk in chunk_iter(pcm, CHUNK_BYTES):
                    if not chunk:
                        continue

                    seq = (seq + 1) & 0xFFFF
                    pkt = build_packet(1, seq, chunk)
                    frame = cobs_encode(pkt) + b"\x00"
                    ser.write(frame)
                    if out_stream is not None:
                        out_stream.write(chunk)

                    sent_bytes += len(chunk)
                    sent_packets += 1
                    time.sleep(len(chunk) / 2.0 / SAMPLE_RATE)

                loop_elapsed = time.monotonic() - loop_start
                print(f"loop finished in {loop_elapsed:.3f}s, replaying...")

    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"serial error: {e}", file=sys.stderr)
        return 1
    finally:
        if out_stream is not None:
            try:
                out_stream.stop()
                out_stream.close()
            except Exception:
                pass

    total = max(time.monotonic() - start, 1e-6)
    kbps = sent_bytes * 8.0 / total / 1000.0
    print(f"Done: packets={sent_packets}, audio_bytes={sent_bytes}, avg_rate={kbps:.1f} kbps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
