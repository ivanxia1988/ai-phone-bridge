import asyncio
import base64
import gzip
import json
import struct

import pytest

from phone_bridge.doubao import DoubaoRealtime, decode_packet, encode_packet, session_config


def server_packet(event, payload, session="session", audio=False, compressed=False):
    raw = payload if audio else json.dumps(payload).encode()
    if compressed:
        raw = gzip.compress(raw)
    identity = session.encode()
    return (bytes([0x11, 0xB4 if audio else 0x94, (0 if audio else 0x10) | compressed, 0])
            + struct.pack(">I", event) + struct.pack(">I", len(identity)) + identity
            + struct.pack(">I", len(raw)) + raw)


def test_official_connection_frame_and_truncated_packets():
    assert encode_packet(1, {}) == bytes([17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125])
    frame = server_packet(150, {"dialog_id": "dialog"})
    assert decode_packet(frame)["payload"] == {"dialog_id": "dialog"}
    for shortened in (frame[:3], frame[:-1], frame + b"extra"):
        with pytest.raises(ValueError):
            decode_packet(shortened)
    assert decode_packet(server_packet(451, {"results": []}, compressed=True))["payload"] == {"results": []}


def test_o2_configuration():
    config = session_config("仅测试")
    assert config["dialog"]["extra"]["model"] == "1.2.1.1"
    assert config["dialog"]["extra"]["enable_user_query_exit"] is True
    assert config["dialog"]["extra"]["enable_conversation_truncate"] is True
    assert config["asr"]["extra"]
    assert config["tts"]["audio_config"]["format"] == "pcm_s16le"


def test_phone_style_is_sent_in_handshake_without_replacing_task_or_voice(monkeypatch):
    async def scenario():
        frames = [server_packet(50, {}, session='connect'), server_packet(150, {'dialog_id':'d'})]
        sent = []
        class Socket:
            async def recv(self):
                return frames.pop(0)
            async def send(self, packet):
                sent.append(packet)
            async def close(self):
                pass
        class Context:
            async def __aenter__(self):
                return Socket()
        monkeypatch.setattr('phone_bridge.doubao.connect', lambda *args, **kwargs: Context())
        task = '了解候选人的机会意向，不介绍未经确认的职位。'
        model = DoubaoRealtime(task, 'synthetic-only', api_key='synthetic-only', voice_id='S_test_ivan')
        await model.open()
        config = decode_packet(sent[1])['payload']
        assert task in config['dialog']['system_role']
        assert config['tts']['speaker'] == 'S_test_ivan'
        style = config['dialog']['speaking_style']
        assert all(word in style for word in ('好奇', '顾虑', '停顿', '播报腔'))
        assert 'tts_2.0_model' not in config['tts'].get('extra', {})
        await model.close()
    asyncio.run(scenario())


def test_length_limit_counts_actual_speaking_style_and_preserves_summary_mode():
    style = session_config('测试')['dialog']['speaking_style']
    role = '测' * (4000 - len(style))
    assert session_config('', system_role=role)['dialog']['system_role'] == role
    with pytest.raises(ValueError, match='4000'):
        session_config('', system_role=role + '测')
    assert session_config('总结', text_only=True)['dialog']['speaking_style'] == '简短自然，普通话。'


def test_doubao_events_preserve_audio_reply_and_exit_order():
    async def scenario():
        realtime = DoubaoRealtime("测试", "test-only", app_id="test-app")
        frames = [server_packet(350, {"reply_id": "r1", "text": "你好"}),
                  server_packet(352, b"\x01\x00" * 320, audio=True),
                  server_packet(359, {"reply_id": "r1", "status_code": "20000002"})]
        class Socket:
            async def recv(self):
                return frames.pop(0)
        realtime.socket = Socket()
        assert (await realtime.receive())['transcript'] == '你好'
        audio = await realtime.receive()
        assert audio["item_id"] == "r1"
        assert base64.b64decode(audio["delta"]) == b"\x01\x00" * 320
        assert (await realtime.receive())["type"] == "response.output_audio.done"
        assert (await realtime.receive())['transcript'] == '你好'
        assert (await realtime.receive())["name"] == "end_call"
    asyncio.run(scenario())


def test_doubao_asr_final_and_interrupt_truncation():
    async def scenario():
        realtime = DoubaoRealtime("测试", "test-only", app_id="test-app")
        frames = [server_packet(450, {"question_id": "q1"}),
                  server_packet(451, {"results": [{"text": "测试一下", "is_interim": True}]}),
                  server_packet(451, {"results": [{"text": "测试一下。", "is_interim": False}]}),
                  server_packet(459, {})]
        sent = []
        class Socket:
            async def recv(self):
                return frames.pop(0)
            async def send(self, packet):
                sent.append(packet)
        realtime.socket = Socket()
        assert (await realtime.receive())["type"] == "input_audio_buffer.speech_started"
        assert (await realtime.receive())["transcript"] == "测试一下"
        assert (await realtime.receive())["transcript"] == "测试一下。"
        text = await realtime.receive()
        assert text["transcript"] == "测试一下。"
        assert text["item_id"] == "q1"
        await realtime.truncate("r1", 500)
        assert decode_packet(sent[0])["payload"] == {"item_id": "r1", "audio_end_ms": 460}
    asyncio.run(scenario())


def test_audio_upload_coalesces_into_twenty_ms_frames():
    async def scenario():
        realtime = DoubaoRealtime("测试", "test-only", app_id="test-app")
        sent = []
        class Socket:
            async def send(self, packet):
                sent.append(packet)
        realtime.socket = Socket()
        source = b"\x01\x00" * 640
        await realtime.append(source[:200])
        assert sent == []
        await realtime.append(source[200:])
        assert len(sent) == 2
        assert b"".join(decode_packet(packet)["payload"] for packet in sent) == source
        assert all(decode_packet(packet)["event"] == 200 for packet in sent)
    asyncio.run(scenario())


def test_handshake_waits_for_session_acceptance(monkeypatch):
    async def scenario():
        frames = [server_packet(50, {}, session="connect"),
                  server_packet(150, {"dialog_id": "d"})]
        sent = []
        class Socket:
            async def recv(self):
                return frames.pop(0)
            async def send(self, packet):
                sent.append(packet)
            async def close(self):
                pass
        class Context:
            async def __aenter__(self):
                return Socket()
        monkeypatch.setattr("phone_bridge.doubao.connect", lambda *args, **kwargs: Context())
        realtime = DoubaoRealtime("测试", "test-only", app_id="test-app")
        await realtime.open()
        assert [decode_packet(packet)["event"] for packet in sent] == [1, 100]
        assert decode_packet(sent[1])["payload"]["dialog"]["extra"]["model"] == "1.2.1.1"
        await realtime.close()
        assert [decode_packet(packet)["event"] for packet in sent][-2:] == [102, 2]
    asyncio.run(scenario())


def test_failed_session_exposes_code_without_provider_message():
    packet = decode_packet(server_packet(153, {"status_code": "45000000", "error": "never expose headers"}))
    with pytest.raises(RuntimeError, match="^doubao_error_45000000$"):
        DoubaoRealtime.check_error(packet)


def test_task_is_never_silently_truncated():
    task = "测" * 3450 + "不要承诺付款"
    assert task in session_config(task)["dialog"]["system_role"]
    with pytest.raises(ValueError, match="4000"):
        session_config("测" * 4000)


def test_final_asr_is_retained_when_end_event_never_arrives():
    async def scenario():
        realtime = DoubaoRealtime("测试", "test-only", app_id="test-app")
        frames = [server_packet(450, {"question_id": "q-final"}),
                  server_packet(451, {"results": [{"text": "我确认收到", "is_interim": False}]})]
        waiting = asyncio.Event()
        class Socket:
            async def recv(self):
                if frames:
                    return frames.pop(0)
                waiting.set()
                await asyncio.Future()
        realtime.socket = Socket()
        await realtime.receive()
        partial = await realtime.receive()
        assert partial['transcript'] == '我确认收到'
        receiver = asyncio.create_task(realtime.receive())
        await waiting.wait()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        final = realtime.final_transcript()
        assert final["text"] == "我确认收到"
        assert final["item_id"] == "q-final"
        assert final["source"] == "final_451_before_asr_ended"
        assert realtime.final_transcript() is None
        realtime.asr_text, realtime.asr_final = "未完成的猜测", False
        assert realtime.final_transcript() is None
    asyncio.run(scenario())


def test_doubao_text_updates_arrive_before_chat_ended():
    async def scenario():
        realtime = DoubaoRealtime('测试', 'synthetic-only')
        frames = [server_packet(550, {'reply_id':'r1','content':'你好'}),
                  server_packet(550, {'reply_id':'r1','content':'，请问方便吗？'}),
                  server_packet(559, {'reply_id':'r1'})]
        class Socket:
            async def recv(self):
                return frames.pop(0)
        realtime.socket = Socket()
        first = await realtime.receive()
        assert first['type'] == 'response.output_audio_transcript.partial'
        assert first['transcript'] == '你好'
        assert len(frames) == 2
        assert (await realtime.receive())['transcript'] == '你好，请问方便吗？'
        assert (await realtime.receive())['type'] == 'response.output_audio_transcript.done'
    asyncio.run(scenario())


def test_new_console_api_key_does_not_send_legacy_credentials(monkeypatch):
    monkeypatch.setenv("DOUBAO_APP_ID", "stale-legacy-app")
    monkeypatch.setenv("DOUBAO_ACCESS_TOKEN", "stale-legacy-token")
    realtime = DoubaoRealtime("测试", "test-only", api_key="new-console-test-key")
    headers = realtime.auth_headers()
    assert headers["X-Api-Key"] == "new-console-test-key"
    assert headers["X-Api-Resource-Id"] == "volc.speech.dialog"
    assert "X-Api-App-ID" not in headers
    assert "X-Api-Access-Key" not in headers
    assert "X-Api-App-Key" not in headers


def test_legacy_auth_remains_available(monkeypatch):
    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)
    realtime = DoubaoRealtime("测试", "test-token", app_id="legacy-app")
    headers = realtime.auth_headers()
    assert headers["X-Api-App-ID"] == "legacy-app"
    assert headers["X-Api-Access-Key"] == "test-token"
    assert "X-Api-Key" not in headers


def test_custom_voice_id_is_used_in_session_config(monkeypatch):
    monkeypatch.setenv("DOUBAO_VOICE", "S_test_custom_voice")
    config = session_config("音色测试")
    assert config["tts"]["speaker"] == "S_test_custom_voice"
    assert config["dialog"]["extra"]["model"] == "1.2.1.1"


def test_per_call_voice_overrides_environment_without_changing_it(monkeypatch):
    monkeypatch.setenv("DOUBAO_VOICE", "S_environment_voice")
    assert session_config("测试", voice_id="S_selected_ivan")["tts"]["speaker"] == "S_selected_ivan"
    assert session_config("测试")["tts"]["speaker"] == "S_environment_voice"
    with pytest.raises(ValueError):
        session_config("测试", voice_id="bad\nvoice")
