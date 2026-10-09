import asyncio
import json

from phone_bridge.realtime import Realtime


def test_realtime_waits_for_session_updated_before_sending_audio(monkeypatch):
    sent = []

    class Socket:
        def __init__(self):
            self.events = iter([{"type": "session.created"}, {"type": "session.updated"}])
        async def send(self, message):
            sent.append(json.loads(message))
        async def recv(self):
            return json.dumps(next(self.events))
    class Context:
        async def __aenter__(self):
            return Socket()
        async def __aexit__(self, *args):
            pass
    def connect(url, **kwargs):
        assert url.startswith("wss://api.openai.com/v1/realtime?model=")
        assert kwargs["additional_headers"]["Authorization"] == "Bearer test-only"
        return Context()
    monkeypatch.setattr("phone_bridge.realtime.connect", connect)

    async def scenario():
        realtime = Realtime("询问营业时间", "test-only")
        await realtime.open()
        await realtime.append(bytes(960))
        await realtime.truncate("item", 1500)
        await realtime.close()
    asyncio.run(scenario())
    assert sent[0]["type"] == "session.update"
    assert sent[0]["session"]["audio"]["input"]["format"]["rate"] == 24000
    assert sent[1]["type"] == "input_audio_buffer.append"
    assert sent[2]["type"] == "conversation.item.truncate"
    assert sent[2]["audio_end_ms"] == 1460
