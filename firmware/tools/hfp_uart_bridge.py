#!/usr/bin/env python3
import argparse
import struct
import sys
import threading
import time
from collections import Counter

import serial


def cobs_encode(data: bytes) -> bytes:
    if not data:
        return b"\x01"

    out = bytearray()
    code_index = 0
    out.append(0)
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


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty cobs frame")

    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            raise ValueError("zero code")
        i += 1
        copy_len = code - 1
        if i + copy_len > len(data):
            raise ValueError("truncated")
        out.extend(data[i : i + copy_len])
        i += copy_len
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


def build_packet(direction: int, seq: int, payload: bytes) -> bytes:
    if direction not in (0, 1, 2):
        raise ValueError("direction must be 0, 1 or 2")
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    header = struct.pack("<BHH", direction, seq & 0xFFFF, len(payload))
    return header + payload


def parse_packet(pkt: bytes):
    if len(pkt) < 5:
        raise ValueError("packet too short")
    direction, seq, length = struct.unpack_from("<BHH", pkt, 0)
    payload = pkt[5:]
    if len(payload) != length:
        raise ValueError("length mismatch")
    return direction, seq, payload


def try_import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore

        return sd
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM17")
    ap.add_argument("--baud", type=int, default=1500000)
    ap.add_argument("--dump-downlink", default="downlink.bin")
    ap.add_argument("--play", action="store_true", help="play downlink audio locally (PCM16LE)")
    ap.add_argument("--mic", action="store_true", help="capture microphone and inject uplink audio (PCM16LE)")
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument(
        "--ul-chunk-bytes",
        type=int,
        default=240,
        help="uplink chunk size (bytes, PCM16LE; will be rounded down to even)",
    )
    ap.add_argument("--stats", action="store_true", help="periodically print link statistics")
    ap.add_argument("--stats-interval", type=float, default=2.0, help="seconds between stats prints")
    ap.add_argument(
        "--cmd",
        action="append",
        default=[],
        help="send control command to ESP (repeatable): hangup, dtmf:5, audio_on, audio_off, query_call",
    )
    ap.add_argument(
        "--stdin-cmd",
        action="store_true",
        help="enable interactive command input from stdin",
    )
    args = ap.parse_args()

    # Non-blocking serial I/O to avoid adding up to `timeout` worth of latency.
    ser = serial.Serial(args.port, args.baud, timeout=0, write_timeout=0)

    dump = open(args.dump_downlink, "ab", buffering=0)

    sd = try_import_sounddevice() if (args.play or args.mic) else None
    if (args.play or args.mic) and sd is None:
        print("sounddevice not available: pip install sounddevice", file=sys.stderr)

    # Downlink raw PCM queue for playback (pcm16le mode)
    dl_pcm_q = bytearray()
    dl_pcm_lock = threading.Lock()

    rx_buf = bytearray()
    ul_seq = 0
    ctrl_seq = 0

    tx_lock = threading.Lock()
    ul_tail = b""

    stop_evt = threading.Event()

    # 16 kHz PCM16LE: 120 frames == 7.5 ms, 240 bytes.
    # Keep chunks small to reduce buffering latency.
    audio_block_frames = 120
    playback_chunk_bytes = 240 * 4

    # Stats
    t0 = time.monotonic()
    last_stats_t = t0
    dl_pkt = 0
    dl_bytes = 0
    dl_len_hist = Counter()
    dl_bad_cobs = 0
    dl_bad_pkt = 0
    ul_pkt = 0
    ul_bytes = 0
    ul_write_drop = 0
    ctrl_pkt = 0
    ctrl_bad = 0

    def send_uplink_payload(payload: bytes):
        nonlocal ul_seq
        nonlocal ul_pkt, ul_bytes
        nonlocal ul_write_drop
        if not payload:
            return
        ul_pkt += 1
        ul_bytes += len(payload)
        ul_seq = (ul_seq + 1) & 0xFFFF
        pkt = build_packet(1, ul_seq, payload)
        frame = cobs_encode(pkt) + b"\x00"
        with tx_lock:
            try:
                ser.write(frame)
            except (serial.SerialTimeoutException, OSError):
                ul_write_drop += 1

    def normalize_control_command(text: str):
        cmd = text.strip()
        if not cmd:
            return None

        low = cmd.lower()
        if low in ("hangup", "hu", "end", "endcall"):
            return "hangup"
        if low in ("audio_on", "aon", "cona"):
            return "audio_on"
        if low in ("audio_off", "aoff", "disa"):
            return "audio_off"
        if low in ("query_call", "status", "qs"):
            return "query_call"

        tone = None
        if low.startswith("dtmf:") and len(cmd) == 6:
            tone = cmd[-1]
        elif low.startswith("dtmf ") and len(cmd) == 6:
            tone = cmd[-1]

        if tone is None:
            return None

        if tone in "0123456789*#":
            return f"dtmf:{tone}"
        up = tone.upper()
        if up in "ABCD":
            return f"dtmf:{up}"
        return None

    def send_control_command(raw_cmd: str):
        nonlocal ctrl_seq, ctrl_pkt, ctrl_bad
        cmd = normalize_control_command(raw_cmd)
        if not cmd:
            ctrl_bad += 1
            print(f"[ctrl] invalid command: {raw_cmd}", file=sys.stderr)
            return False

        ctrl_seq = (ctrl_seq + 1) & 0xFFFF
        pkt = build_packet(2, ctrl_seq, cmd.encode("ascii"))
        frame = cobs_encode(pkt) + b"\x00"
        with tx_lock:
            try:
                ser.write(frame)
            except (serial.SerialTimeoutException, OSError) as e:
                print(f"[ctrl] write failed for {cmd}: {e}", file=sys.stderr)
                return False

        ctrl_pkt += 1
        print(f"[ctrl] sent: {cmd}")
        return True

    def maybe_print_stats(now_t: float):
        nonlocal last_stats_t
        if not args.stats:
            return
        if args.stats_interval <= 0:
            return
        if now_t - last_stats_t < args.stats_interval:
            return

        elapsed = now_t - t0
        dl_kbps = (dl_bytes * 8.0) / max(elapsed, 1e-6) / 1000.0
        ul_kbps = (ul_bytes * 8.0) / max(elapsed, 1e-6) / 1000.0

        top_lens = ", ".join(f"{k}:{v}" for k, v in dl_len_hist.most_common(6)) or "(none)"

        print(
            "[stats] "
            f"t={elapsed:.1f}s "
            f"DL pkt={dl_pkt} bytes={dl_bytes} kbps={dl_kbps:.1f} "
            f"UL pkt={ul_pkt} bytes={ul_bytes} kbps={ul_kbps:.1f} "
            f"bad_cobs={dl_bad_cobs} bad_pkt={dl_bad_pkt} ul_write_drop={ul_write_drop} "
            f"ctrl_sent={ctrl_pkt} ctrl_bad={ctrl_bad}"
        )
        print(f"[stats] DL payload_len top: {top_lens}")

        last_stats_t = now_t

    def audio_playback_worker():
        if sd is None:
            print("playback disabled (sounddevice missing)", file=sys.stderr)
            return

        try:
            stream = sd.RawOutputStream(
                samplerate=args.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=audio_block_frames,
                latency="low",
            )
            stream.start()
        except Exception as e:
            print(f"failed to open audio output: {e}", file=sys.stderr)
            return

        try:
            while not stop_evt.is_set():
                with dl_pcm_lock:
                    if dl_pcm_q:
                        pcm = bytes(dl_pcm_q[:playback_chunk_bytes])
                        del dl_pcm_q[:playback_chunk_bytes]
                    else:
                        pcm = b""
                if not pcm:
                    time.sleep(0.002)
                    continue
                stream.write(pcm)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def mic_capture_and_send_worker():
        if sd is None:
            print("mic disabled (sounddevice missing)", file=sys.stderr)
            return

        pcm_q = bytearray()
        pcm_lock = threading.Lock()
        pcm_evt = threading.Event()

        ul_chunk = int(args.ul_chunk_bytes)
        if ul_chunk <= 0:
            ul_chunk = 240
        ul_chunk &= ~1  # int16 alignment
        if ul_chunk <= 0:
            ul_chunk = 240

        # Align mic capture blocksize to uplink chunk (frames = bytes/2).
        # Smaller blocks reduce buffering latency but may be less stable on some devices.
        input_block_frames = ul_chunk // 2
        if input_block_frames < 60:
            input_block_frames = 60

        def cb(indata, frames, time_info, status):
            # indata is a raw bytes-like buffer for RawInputStream
            _ = (frames, time_info, status)
            with pcm_lock:
                pcm_q.extend(indata)
            pcm_evt.set()

        try:
            istream = sd.RawInputStream(
                samplerate=args.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=input_block_frames,
                latency="low",
                callback=cb,
            )
            istream.start()
        except Exception as e:
            print(f"failed to open microphone: {e}", file=sys.stderr)
            return

        nonlocal ul_tail

        try:
            while not stop_evt.is_set():
                # Wait for mic callback to enqueue data (reduces polling latency).
                pcm_evt.wait(0.05)
                pcm_evt.clear()

                with pcm_lock:
                    if pcm_q:
                        chunk = bytes(pcm_q)
                        pcm_q.clear()
                    else:
                        chunk = b""

                if chunk:
                    ul_tail += chunk

                while len(ul_tail) >= ul_chunk:
                    send_uplink_payload(ul_tail[:ul_chunk])
                    ul_tail = ul_tail[ul_chunk:]
        finally:
            try:
                istream.stop()
                istream.close()
            except Exception:
                pass

    pb_th = None
    if args.play:
        pb_th = threading.Thread(target=audio_playback_worker, daemon=True)
        pb_th.start()

    mic_th = None
    if args.mic:
        mic_th = threading.Thread(target=mic_capture_and_send_worker, daemon=True)
        mic_th.start()

    cmd_th = None

    def stdin_command_worker():
        while not stop_evt.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                time.sleep(0.05)
                continue
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("quit", "exit"):
                stop_evt.set()
                return
            send_control_command(text)

    if args.stdin_cmd:
        cmd_th = threading.Thread(target=stdin_command_worker, daemon=True)
        cmd_th.start()

    print(f"Listening on {args.port} @ {args.baud}")
    print(f"Downlink dump: {args.dump_downlink}")
    if args.play:
        print(f"Playback enabled (pcm16le) @ {args.sample_rate} Hz")
    if args.mic:
        print(f"Microphone injection enabled (pcm16le) @ {args.sample_rate} Hz")
    if args.stdin_cmd:
        print("Control stdin enabled: hangup | dtmf:<0-9*#A-D> | audio_on | audio_off | query_call")

    for cmd in args.cmd:
        send_control_command(cmd)

    try:
        while True:
            now_t = time.monotonic()
            n = ser.in_waiting
            chunk = ser.read(4096 if n > 4096 else n) if n else b""
            if chunk:
                rx_buf.extend(chunk)

            while True:
                try:
                    idx = rx_buf.index(0)
                except ValueError:
                    break

                frame = bytes(rx_buf[:idx])
                del rx_buf[: idx + 1]
                if not frame:
                    continue

                try:
                    pkt = cobs_decode(frame)
                    direction, seq, payload = parse_packet(pkt)
                except Exception:
                    dl_bad_cobs += 1
                    continue

                if direction == 2:
                    if payload:
                        try:
                            text = payload.decode("utf-8", errors="replace")
                        except Exception:
                            text = repr(payload)
                        if text.startswith("state:"):
                            print(f"[state] seq={seq} {text}")
                        elif text.startswith("resp:"):
                            print(f"[resp] seq={seq} {text}")
                        else:
                            print(f"[evt] seq={seq} {text}")
                    continue

                if direction != 0:
                    continue

                if payload:
                    dl_pkt += 1
                    dl_bytes += len(payload)
                    dl_len_hist[len(payload)] += 1

                    dump.write(payload)
                    if args.play:
                        # Interpret payload as raw PCM int16le.
                        pcm = payload
                        if len(pcm) & 1:
                            pcm = pcm[:-1]
                        if pcm:
                            with dl_pcm_lock:
                                dl_pcm_q.extend(pcm)

            if not chunk:
                time.sleep(0.001)

            maybe_print_stats(time.monotonic())

    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        try:
            dump.close()
        except Exception:
            pass
        ser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
