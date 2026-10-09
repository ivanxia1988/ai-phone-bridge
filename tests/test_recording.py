import struct
import wave

from phone_bridge.recording import CallRecording


def test_tracks_keep_silence_between_packets(tmp_path):
    recording = CallRecording(tmp_path, 8000)
    recording.write("remote", struct.pack("<80h", *([123] * 80)), 0.0)
    recording.write("remote", struct.pack("<80h", *([456] * 80)), 0.03)
    recording.write("agent", struct.pack("<80h", *([789] * 80)), 0.02)
    recording.close(0.05)
    with wave.open(str(tmp_path / "remote.wav")) as remote:
        values = struct.unpack("<400h", remote.readframes(400))
        assert remote.getframerate() == 8000
        assert values[:80] == (123,) * 80
        assert values[80:240] == (0,) * 160
        assert values[240:320] == (456,) * 80
    with wave.open(str(tmp_path / "agent.wav")) as agent:
        values = struct.unpack("<400h", agent.readframes(400))
        assert values[160:240] == (789,) * 80
