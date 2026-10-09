import numpy as np

from phone_bridge.audio import Playback, Resampler


def test_stream_resampling_retains_duration_across_small_packets():
    source = np.zeros(8000, dtype="<i2").tobytes()
    converter = Resampler(8000, 24000)
    parts = [converter.convert(source[i:i + 160]) for i in range(0, len(source), 160)]
    parts.append(converter.convert(b"", final=True))
    assert len(b"".join(parts)) == 24000 * 2


def test_interrupt_discards_generated_audio_and_tracks_sent_duration():
    playback = Playback(8000)
    playback.append("item1", bytes(8000 * 2))
    item, packet = playback.pop(160)
    assert item == "item1" and len(packet) == 160
    interrupted = playback.interrupt()
    assert interrupted == ("item1", 10)
    assert playback.pop(160) is None
    playback.append("item1", bytes(160))
    assert playback.pop(160) is None
    playback.append("item2", bytes(160))
    assert playback.pop(160)[0] == "item2"


def test_new_speech_does_not_truncate_completed_playback():
    playback = Playback(8000)
    playback.append("done", bytes(160))
    playback.finish("done")
    playback.pop(160)
    assert playback.interrupt() == (None, 0)


def test_network_audio_deltas_are_combined_into_continuous_phone_packets():
    playback = Playback(16000)
    playback.append("item", b"\x11\x22" * 50)
    playback.append("item", b"\x33\x44" * 150)
    assert playback.pop(320) == ("item", b"\x11\x22" * 50 + b"\x33\x44" * 110)
    assert playback.pop(320) is None  # Wait for the next delta, not a short packet.
    playback.append("item", b"\x55\x66" * 120)
    assert playback.pop(320) == ("item", b"\x33\x44" * 40 + b"\x55\x66" * 120)
