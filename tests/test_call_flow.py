import asyncio
import base64
import json
import wave

from phone_bridge.service import PhoneService, PhoneCall


class FakeRealtime:
    def __init__(self, task, key):
        self.events = asyncio.Queue()
        self.inputs = []
        self.truncations = []

    async def open(self):
        pass

    async def greet(self):
        self.events.put_nowait({"type": "response.output_audio.delta", "item_id": "a1",
                               "delta": base64.b64encode(bytes(24000)).decode()})

    async def receive(self):
        return await self.events.get()

    async def append(self, pcm):
        self.inputs.append(pcm)

    async def truncate(self, item, sent_ms):
        self.truncations.append((item, sent_ms))

    async def close(self):
        pass


def test_complete_call_and_interrupt_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    async def scenario():
        service = PhoneService("fake", realtime_factory=FakeRealtime,
                               summary_fn=lambda *_: "模拟总结")
        service.loop = asyncio.get_running_loop()
        service.state.update("state:conn=3 audio=0 call=0 setup=0 rate=8000 firmware=1")

        class Transport:
            def __init__(self):
                self.commands = []
                self.audio = []

            def control(self, text):
                self.commands.append(text)
                if text.startswith("dial:"):
                    service._receive(2, b"resp:dial queued")
                    service._receive(2, b"evt:setup=2")
                    service._receive(2, b"evt:call_active")
                    service._receive(2, b"evt:audio_connected rate=8000")
                elif text == "query_call":
                    service._receive(2, b"state:conn=3 audio=0 call=0 setup=0 rate=8000 firmware=1")

            def send(self, direction, payload):
                assert direction == 1
                self.audio.append(payload)

        service.transport = Transport()
        async def initial_refresh():
            pass
        service.refresh = initial_refresh
        call = PhoneCall(service, "+8613812345678", "仅测试", 10)
        service.current = call

        async def remote():
            while not call.audio_ready:
                await asyncio.sleep(0.01)
            for _ in range(15):
                service._receive(0, bytes(160))
                await asyncio.sleep(0.01)
            call.realtime.events.put_nowait({"type": "input_audio_buffer.speech_started"})
            await asyncio.sleep(0.08)
            service._receive(2, b"evt:call_idle")
            service._receive(2, b"evt:setup=0")

        await asyncio.gather(call.run(), remote())
        assert call.result["state"] == "completed"
        assert call.result["hangup_confirmed"]
        assert call.realtime.inputs
        assert call.realtime.truncations[0][0] == "a1"
        assert call.result["audio_bytes"]["remote"] == 2400
        assert service.transport.commands[0] == "dial:+8613812345678"
        assert "flush" in service.transport.commands
        assert service.current is None
        with wave.open(str(call.path / "remote.wav")) as audio:
            assert audio.getnframes() >= 1200
        assert (call.path / "summary.md").read_text() == "模拟总结"
        assert json.loads((call.path / "result.json").read_text())["state"] == "completed"

    asyncio.run(scenario())


def test_model_initialization_failure_never_dials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    class BrokenRealtime(FakeRealtime):
        async def open(self):
            raise RuntimeError("simulated API rejection")

    async def scenario():
        service = PhoneService("fake", realtime_factory=BrokenRealtime,
                               summary_fn=lambda *_: "未拨号")
        call = PhoneCall(service, "13812345678", "测试", 10)
        service.current = call
        await call.run()
        assert not call.dial_sent
        assert call.result["state"] == "failed"
        assert service.current is None

    asyncio.run(scenario())


def test_cancel_during_delayed_dial_always_sends_hangup(tmp_path, monkeypatch):
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    async def scenario():
        service = PhoneService("fake")
        service.state.conn = 3
        call = PhoneCall(service, "13812345678", "测试", 10)
        call.dial_sent = True
        call.dial_accepted = True
        commands = []
        class Transport:
            def control(self, command):
                commands.append(command)
        service.transport = Transport()
        queries = 0
        async def refresh():
            nonlocal queries
            queries += 1
            service.state.setup = 2 if queries == 1 else 0
        service.refresh = refresh
        await call.ensure_hangup()
        assert "hangup" in commands
        assert call.result["hangup_confirmed"]

    asyncio.run(scenario())


def test_busy_dial_does_not_record_or_hang_up_existing_call(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    async def scenario():
        service = PhoneService("fake", realtime_factory=FakeRealtime,
                               summary_fn=lambda *_: "拒绝拨号")
        service.state.conn = 3
        commands = []
        class Transport:
            def control(self, command):
                commands.append(command)
                if command.startswith("dial:"):
                    service._receive(2, b"resp:dial busy")
                    service._receive(2, b"evt:call_active")
                    service._receive(2, b"evt:audio_connected rate=8000")
        service.transport = Transport()
        async def refresh():
            pass
        service.refresh = refresh
        call = PhoneCall(service, "13812345678", "测试", 10)
        service.current = call
        await call.run()
        assert call.result["state"] == "failed"
        assert commands == ["dial:13812345678"]
        assert not (call.path / "remote.wav").exists()
        assert service.state.active

    asyncio.run(scenario())


def test_cancel_during_final_refresh_never_dials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    async def scenario():
        service = PhoneService("fake", realtime_factory=FakeRealtime,
                               summary_fn=lambda *_: "取消")
        call = PhoneCall(service, "13812345678", "测试", 10)
        service.current = call
        async def refresh():
            call.stop.set()
        service.refresh = refresh
        await call.run()
        assert not call.dial_sent
        assert call.result["state"] == "cancelled_before_dial"
    asyncio.run(scenario())


def test_async_phone_busy_after_local_queue_never_takes_existing_call(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("phone_bridge.service.ROOT", tmp_path)

    async def scenario():
        service = PhoneService("fake", realtime_factory=FakeRealtime,
                               summary_fn=lambda *_: "手机拒绝")
        service.state.conn = 3
        commands = []
        class Transport:
            def control(self, command):
                commands.append(command)
                if command.startswith("dial:"):
                    service._receive(2, b"resp:dial queued")
                    service._receive(2, b"evt:at code=3")
                    service._receive(2, b"evt:call_active")
                    service._receive(2, b"evt:audio_connected rate=8000")
        service.transport = Transport()
        async def refresh():
            pass
        service.refresh = refresh
        call = PhoneCall(service, "13812345678", "测试", 10)
        service.current = call
        await call.run()
        assert call.result["state"] == "failed"
        assert commands == ["dial:13812345678"]
        assert not call.audio_ready
        assert not (call.path / "remote.wav").exists()
        assert service.state.active
    asyncio.run(scenario())
