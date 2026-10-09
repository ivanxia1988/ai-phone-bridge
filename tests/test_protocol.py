import pytest

from phone_bridge.protocol import FrameDecoder, encode_frame, validate_number


def test_fragmented_frames_survive_boot_text_and_bad_frame():
    decoder = FrameDecoder()
    data = b"boot message\x00\x02\x00" + encode_frame(0, 7, bytes(range(256)))
    frames = []
    for byte in data:
        frames.extend(decoder.feed(bytes([byte])))
    assert frames == [(0, 7, bytes(range(256)))]


def test_oversize_frame_discarded_until_delimiter():
    decoder = FrameDecoder()
    valid = encode_frame(2, 8, b"status")
    assert decoder.feed(b"x" * 2000 + valid) == []
    assert decoder.feed(valid) == [(2, 8, b"status")]


@pytest.mark.parametrize("number", ["", "+", "12;ATD123", "123\x00", "123\n", "*#06#", "1" * 32])
def test_reject_non_phone_commands(number):
    with pytest.raises(ValueError):
        validate_number(number)


def test_keep_international_number():
    assert validate_number("+8613812345678") == "+8613812345678"
