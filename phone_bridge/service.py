import asyncio
import base64
import contextlib
import json
import os
import re
import signal
import time
import uuid
from pathlib import Path

from .credentials import ENV_KEYS, validate_provider, validate_key
from .audio import Playback, Resampler
from .live_transcript import LiveTranscript
from .protocol import validate_number
from .realtime import Realtime
from .doubao import DoubaoRealtime, summarize_transcripts, validate_voice, session_config as doubao_config
from .recording import CallRecording
from .state import DeviceState
from .summary import summarize
from .transport import SerialTransport

ROOT = Path(__file__).resolve().parent.parent
SOCKET = ROOT / "work" / "phone.sock"


def safe_error(exc):
    known = {"phone_not_idle_before_dial", "phone_disconnected", "dial_rejected",
             "phone_rejected_call", "ended_before_answer", "no_answer_or_audio_timeout",
             "invalid_doubao_audio", "doubao_session_closed", "audio_missing_item_id"}
    message = str(exc)
    if message in known or re.fullmatch(r"doubao_error_(?:http_\d{3}|\d+|unknown)", message):
        return message
    return type(exc).__name__


class PhoneService:
    def __init__(self, port: str, *, realtime_factory=None, summary_fn=None):
        self.port = port
        self.state = DeviceState()
        self.transport = None
        self.loop = None
        self.current = None
        self.monitor_task = None
        self.tasks = {}
        self.provider = os.getenv("PHONE_VOICE_PROVIDER", "openai")
        if self.provider not in ("openai", "doubao"):
            raise ValueError("PHONE_VOICE_PROVIDER 必须为 openai 或 doubao")
        self.realtime_factory = realtime_factory or (DoubaoRealtime if self.provider == "doubao" else Realtime)
        self.summary_fn = summary_fn or (summarize_transcripts if self.provider == "doubao" else summarize)
        self.messages = []
        self.last_state = 0.0
        self.closed = asyncio.Event()
        self.reconnecting = False
        self.configuration_revision = 0

    def received(self, direction, payload):
        self.loop.call_soon_threadsafe(self._receive, direction, payload)

    def _receive(self, direction, payload):
        if direction == 2:
            message = payload.decode("ascii", errors="replace")
            self.state.update(message)
            if message.startswith("state:"):
                self.last_state = time.monotonic()
            self.messages.append((time.monotonic(), message))
            self.messages = self.messages[-100:]
            if self.current:
                call = self.current
                if message.startswith(("state:", "evt:", "resp:")):
                    events = call.result.setdefault("device_events", [])
                    events.append(message[:200])
                    del events[:-100]
                if call.dial_sent:
                    if message.startswith("resp:dial "):
                        call.dial_accepted = message == "resp:dial queued"
                        call.dial_rejected = not call.dial_accepted
                    elif message in ("evt:setup=2", "evt:setup=3"):
                        call.outgoing_seen = True
                    elif not call.audio_ready and (message == "evt:setup=1" or
                            (message.startswith("evt:at code=") and message != "evt:at code=0")):
                        call.dial_rejected = True
                self.current.controls.put_nowait(message)
        elif direction == 0 and self.current and self.current.audio_ready:
            call = self.current
            rate = call.result["rate"]
            call.recording.write("remote", payload, max(0, call.seconds() - len(payload) / (rate * 2)))
            call.result["audio_bytes"]["remote"] += len(payload)
            try:
                self.current.incoming.put_nowait(payload)
            except asyncio.QueueFull:
                self.current.error = "电脑音频输入积压"
                self.current.stop.set()

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.transport = SerialTransport(self.port, self.received,
                                         int(os.getenv("ESP32_BAUD", "921600")))
        await asyncio.sleep(1.5)
        self.transport.port.write(b"\x00")
        await self.refresh()

    async def refresh(self):
        if not self.transport:
            raise RuntimeError("请先连接 ESP32 设备")
        previous = self.last_state
        for _ in range(10):
            self.transport.control("query_call")
            await asyncio.sleep(0.4)
            if self.last_state > previous:
                return
        raise RuntimeError("没有收到 ESP32 固件状态")

    def status(self):
        return {"service_version": 4, "port": self.port, "firmware": self.state.firmware,
                "firmware_alive": bool((self.current or self.last_state and time.monotonic() - self.last_state < 12) and not self.state.transport_lost),
                "phone_connected": self.state.connected, "call_active": self.state.active, "call_setup": self.state.setup,
                "audio_connected": self.state.audio, "rate": self.state.rate,
                "transport_lost": self.state.transport_lost,
                "openai_key_available": bool(os.getenv("OPENAI_API_KEY")),
                "voice_provider": self.provider,
                "voice_credentials_available": self.credentials_available(),
                "default_voice": (os.getenv("DOUBAO_VOICE", "zh_female_vv_jupiter_bigtts") if self.provider == "doubao" else os.getenv("OPENAI_VOICE", "marin")),
                "reconnecting": self.reconnecting,
                "current_call": {**self.current.result, "live_transcripts": self.current.live.snapshot(),
                                 "elapsed_seconds": self.current.result.get("duration_seconds", round(self.current.seconds(), 2))} if self.current else None}

    def credentials_available(self):
        if self.provider == "doubao":
            return bool(os.getenv("DOUBAO_API_KEY") or
                        os.getenv("DOUBAO_APP_ID") and os.getenv("DOUBAO_ACCESS_TOKEN"))
        return bool(os.getenv("OPENAI_API_KEY"))

    def voice_key(self):
        if not self.credentials_available():
            required = "DOUBAO_API_KEY（旧版 DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN）" if self.provider == "doubao" else "OPENAI_API_KEY"
            raise RuntimeError("未配置 " + required + "，没有拨号")
        if self.provider == "doubao":
            return os.getenv("DOUBAO_API_KEY") or os.environ["DOUBAO_ACCESS_TOKEN"]
        return os.environ["OPENAI_API_KEY"]

    def model(self, task, voice_id=None):
        if voice_id is not None:
            if self.provider != "doubao":
                raise ValueError("当前音色选择仅支持豆包 O2")
            return self.realtime_factory(task, self.voice_key(), voice_id=validate_voice(voice_id))
        return self.realtime_factory(task, self.voice_key())

    async def command(self, request):
        operation = request.get("op")
        if operation in ("call", "check-api") and request.get("provider", self.provider) != self.provider:
            raise RuntimeError("渠道设置已变化，请重新检测所选渠道")
        if operation == "check-api":
            self.voice_key()
            if self.current:
                raise RuntimeError("已有通话正在处理，请稍后验证 API")
            revision, provider = self.configuration_revision, self.provider
            voice_id = request.get("voice_id")
            realtime = self.model("只验证会话连接，不发起电话。", voice_id)
            try:
                await realtime.open()
                if revision != self.configuration_revision:
                    raise RuntimeError("渠道设置已变化，请重新检测所选渠道")
                return {"realtime_api_ready": True,
                        "voice_provider": self.provider,
                        "voice_id": voice_id or self.status()["default_voice"],
                        "model": "1.2.1.1 (O2)" if self.provider == "doubao" else os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")}
            except Exception as exc:
                if revision != self.configuration_revision:
                    raise RuntimeError("渠道设置已变化，请重新检测所选渠道") from None
                raise RuntimeError(provider + " 实时会话连接失败：" + safe_error(exc)) from None
            finally:
                try:
                    await realtime.close()
                except Exception:
                    pass
        if operation == "status":
            if request.get("refresh", True):
                await self.refresh()
            return self.status()
        if operation == "configure":
            if self.current or self.reconnecting or self.state.active or self.state.setup:
                raise RuntimeError("通话或连接正在处理，暂不能修改配置")
            provider = validate_provider(request.get("provider", "doubao"))
            key = request.get("api_key", "")
            if not isinstance(key, str):
                raise ValueError("请输入有效的 API Key")
            if key.strip():
                os.environ[ENV_KEYS[provider]] = validate_key(key)
            elif not os.getenv(ENV_KEYS[provider]):
                raise RuntimeError("该渠道尚未配置 API Key，请先保存")
            self.provider = provider
            self.realtime_factory = DoubaoRealtime if provider == "doubao" else Realtime
            self.summary_fn = summarize_transcripts if provider == "doubao" else summarize
            self.configuration_revision += 1
            return {"voice_credentials_available": True, "voice_provider": provider}
        if operation == "reconnect":
            if self.current or self.state.active or self.state.setup or self.reconnecting:
                raise RuntimeError("已有通话或连接正在处理，暂不能重连")
            port = request.get("port", self.port)
            if not isinstance(port, str) or not port.startswith("/dev/cu.") or len(port) > 200:
                raise ValueError("无效的设备端口")
            self.reconnecting = True
            try:
                if self.transport:
                    self.transport.close()
                self.transport = None
                self.state = DeviceState()
                self.port = port
                await self.start()
                return self.status()
            finally:
                self.reconnecting = False
        if operation == "call":
            number = validate_number(request.get("number", ""))
            task = request.get("task", "")
            limit = request.get("max_seconds", 180)
            if not isinstance(task, str) or not 1 <= len(task.strip()) <= 4000:
                raise ValueError("请提供 1–4000 字的电话委托")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= 600:
                raise ValueError("通话时限必须为 10–600 秒")
            if self.current or self.reconnecting:
                raise RuntimeError("已有通话正在处理")
            self.voice_key()
            if self.provider == "doubao":
                doubao_config(task, voice_id=request.get("voice_id"))
            revision = self.configuration_revision
            await self.refresh()
            if revision != self.configuration_revision:
                raise RuntimeError("渠道设置已变化，请重新检测所选渠道")
            if self.current or self.reconnecting:
                raise RuntimeError("已有通话或连接正在处理")
            if not self.state.connected or self.state.firmware != 1:
                raise RuntimeError("请先让 iPhone 配对并连接 ESP32 Phone Bridge")
            if self.state.active or self.state.setup:
                raise RuntimeError("手机已有通话，不能发起新通话")
            call = PhoneCall(self, number, task, limit, voice_id=request.get("voice_id"))
            self.current = call
            self.tasks[call.id] = asyncio.create_task(call.run())
            return dict(call.result)
        if operation == "hangup":
            if not self.current:
                return {"state": "no_managed_call"}
            self.current.stop.set()
            return {"state": "hangup_requested", "id": self.current.id}
        if operation == "result":
            call_id = request.get("id", "")
            try:
                uuid.UUID(call_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("无效的通话编号") from None
            return json.loads((ROOT / "calls" / call_id / "result.json").read_text(encoding="utf-8"))
        if operation == "stop":
            self.closed.set()
            return {"state": "stopping"}
        raise ValueError("未知操作")

    async def client(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > 16000:
                raise ValueError("请求过大")
            result = await self.command(json.loads(line))
            response = {"ok": True, "result": result}
        except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
            # Expected errors contain no credentials; network errors are handled within calls.
            response = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def monitor_device(self):
        while not self.closed.is_set():
            await asyncio.sleep(3)
            if not self.current and not self.reconnecting and self.transport:
                try:
                    await self.refresh()
                except (RuntimeError, OSError):
                    self.last_state = 0

    async def serve(self):
        import fcntl
        SOCKET.parent.mkdir(parents=True, exist_ok=True)
        lock = (SOCKET.parent / "service.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await self.start()
            SOCKET.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.client, path=str(SOCKET))
            SOCKET.chmod(0o600)
            self.monitor_task = asyncio.create_task(self.monitor_device())
            for sig in (signal.SIGINT, signal.SIGTERM):
                self.loop.add_signal_handler(sig, self.closed.set)
            async with server:
                await self.closed.wait()
            if self.current:
                self.current.stop.set()
                await self.tasks[self.current.id]
        finally:
            if self.monitor_task:
                self.monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.monitor_task
            if self.transport:
                self.transport.close()
            SOCKET.unlink(missing_ok=True)
            lock.close()


class PhoneCall:
    def __init__(self, service, number, task, limit, *, voice_id=None):
        self.service, self.number, self.task, self.limit = service, number, task, limit
        self.voice_id = voice_id
        self.id = str(uuid.uuid4())
        self.path = ROOT / "calls" / self.id
        self.path.mkdir(parents=True, mode=0o700)
        self.result = {"id": self.id, "state": "preparing", "number": number, "task": task,
                       "voice_provider": service.provider,
                       "voice_id": voice_id or service.status()["default_voice"],
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "path": str(self.path), "hangup_confirmed": False,
                       "audio_bytes": {"remote": 0, "agent": 0}}
        self.incoming = asyncio.Queue(maxsize=200)
        self.controls = asyncio.Queue()
        self.stop = asyncio.Event()
        self.error = None
        self.audio_ready = False
        self.recording = None
        self.connected_at = None
        self.playback = None
        self.realtime = None
        self.dial_sent = False
        self.dial_accepted = False
        self.dial_rejected = False
        self.outgoing_seen = False
        self.finish_after_playback = False
        self.finish_empty_since = None
        self.live = LiveTranscript()
        self.save()

    def save(self, state=None):
        if state:
            self.result["state"] = state
        temporary = self.path / "result.tmp"
        temporary.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path / "result.json")

    def seconds(self):
        return max(0, time.monotonic() - (self.connected_at or time.monotonic()))

    async def run(self):
        key = self.service.voice_key()
        workers = []
        try:
            self.realtime = self.service.model(self.task, self.voice_id)
            await self.realtime.open()
            if self.stop.is_set():
                self.save("cancelled_before_dial")
                return
            # The phone may have received another call while the API was initializing.
            await self.service.refresh()
            if self.stop.is_set():
                self.save("cancelled_before_dial")
                return
            if not self.service.state.connected or self.service.state.active or self.service.state.setup:
                raise RuntimeError("phone_not_idle_before_dial")
            self.dial_sent = True
            self.service.transport.control("dial:" + self.number)
            self.save("dialing")
            await self.wait_for_connection()
            if self.stop.is_set():
                self.save("cancelled_before_answer")
            else:
                self.connected_at = time.monotonic()
                rate = self.service.state.rate
                self.result["rate"] = rate
                self.recording = CallRecording(self.path, rate)
                self.playback = Playback(rate)
                self.audio_ready = True
                self.save("talking")
                workers = [asyncio.create_task(self.send_input(rate)),
                           asyncio.create_task(self.receive_model(rate)),
                           asyncio.create_task(self.play_output(rate))]
                await self.realtime.greet()
                await self.monitor()
                self.save("failed" if self.error else "completed")
        except Exception as exc:
            # Avoid logging HTTP/WebSocket exception strings, which may include headers.
            self.error = self.error or safe_error(exc)
            self.save("failed")
        finally:
            self.audio_ready = False
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if self.recording and hasattr(self.realtime, "final_transcript"):
                final = self.realtime.final_transcript()
                if final:
                    self.live.update('remote', final['item_id'], final['text'], self.seconds(), final=True)
                    self.recording.transcript("remote", final.pop("text"), self.seconds(), **final)
            if self.dial_sent:
                await self.ensure_hangup()
            if self.realtime:
                try:
                    await self.realtime.close()
                except Exception:
                    pass
            if self.recording:
                self.result["duration_seconds"] = round(self.seconds(), 2)
                self.recording.close(self.seconds())
            if self.error:
                self.result["error"] = self.error
            self.result["call_state"] = self.result["state"]
            self.save("summarizing")
            try:
                text = await asyncio.to_thread(self.service.summary_fn, self.path, self.task, key, self.result)
                (self.path / "summary.md").write_text(text, encoding="utf-8")
                self.result["summary_path"] = str(self.path / "summary.md")
            except Exception as exc:
                self.result["summary_error"] = type(exc).__name__
                (self.path / "summary.md").write_text("总结生成失败，请复核录音及转写。\n", encoding="utf-8")
            self.save(self.result["call_state"])
            if self.service.current is self:
                self.service.current = None

    async def wait_for_connection(self):
        deadline = time.monotonic() + 50
        setup_seen = False
        while time.monotonic() < deadline:
            if self.stop.is_set():
                return
            state = self.service.state
            if not state.connected:
                raise RuntimeError("phone_disconnected")
            if self.dial_rejected:
                raise RuntimeError("dial_rejected")
            if self.dial_accepted and self.outgoing_seen and state.active and state.audio:
                return
            try:
                message = await asyncio.wait_for(self.controls.get(), 0.3)
            except asyncio.TimeoutError:
                continue
            if message.startswith("resp:dial") and message != "resp:dial queued":
                raise RuntimeError("dial_rejected")
            if message.startswith("evt:at code=") and message != "evt:at code=0":
                raise RuntimeError("phone_rejected_call")
            if message in ("evt:setup=2", "evt:setup=3"):
                setup_seen = True
            if setup_seen and message == "evt:setup=0" and not state.active:
                raise RuntimeError("ended_before_answer")
        raise TimeoutError("no_answer_or_audio_timeout")

    async def guarded(self, action):
        try:
            await action
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = safe_error(exc)
            self.stop.set()

    async def send_input(self, rate):
        async def work():
            converter = Resampler(rate, getattr(self.realtime, "input_rate", 24000))
            while True:
                pcm = await self.incoming.get()
                converted = converter.convert(pcm)
                if converted:
                    await self.realtime.append(converted)
        await self.guarded(work())

    async def receive_model(self, rate):
        async def work():
            converters = {}
            while True:
                event = await self.realtime.receive()
                kind = event["type"]
                item = event.get("item_id", "")
                if kind == "response.output_audio.delta":
                    if not item:
                        raise RuntimeError("audio_missing_item_id")
                    converter = converters.setdefault(item, Resampler(getattr(self.realtime, "output_rate", 24000), rate))
                    pcm = converter.convert(base64.b64decode(event["delta"], validate=True))
                    self.playback.append(item, pcm)
                elif kind == "response.output_audio.done":
                    converter = converters.pop(item, None)
                    if converter:
                        self.playback.append(item, converter.convert(b"", final=True))
                    self.playback.finish(item)
                elif kind == "input_audio_buffer.speech_started":
                    self.finish_after_playback = False
                    self.finish_empty_since = None
                    interrupted, sent_ms = self.playback.interrupt()
                    self.service.transport.control("flush")
                    if interrupted:
                        self.live.interrupt(interrupted)
                        await self.realtime.truncate(interrupted, sent_ms)
                        self.recording.transcript("interrupt", "", self.seconds(), item_id=interrupted,
                                                  sent_audio_ms=sent_ms)
                elif kind in ("conversation.item.input_audio_transcription.delta", "response.output_audio_transcript.delta"):
                    role = 'remote' if kind.startswith('conversation.') else 'agent_generated'
                    self.live.update(role, item, event.get('delta', ''), self.seconds(), delta=True)
                elif kind in ("conversation.item.input_audio_transcription.partial", "response.output_audio_transcript.partial"):
                    role = 'remote' if kind.startswith('conversation.') else 'agent_generated'
                    self.live.update(role, item, event.get('transcript', ''), self.seconds())
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self.live.update('remote', item, event['transcript'], self.seconds(), final=True)
                    self.recording.transcript("remote", event["transcript"], self.seconds(), item_id=item)
                elif kind == "response.output_audio_transcript.done":
                    self.live.update('agent_generated', item, event['transcript'], self.seconds(), final=True)
                    self.recording.transcript("agent_generated", event["transcript"], self.seconds(), item_id=item)
                elif kind == "response.function_call_arguments.done" and event.get("name") == "end_call":
                    self.finish_after_playback = True
                    self.finish_empty_since = None
                    if event.get("call_id"):
                        await self.realtime.send({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": event["call_id"],
                            "output": '{"ending":true}'}})
                elif kind == "error":
                    self.error = self.service.provider + ": " + event.get("error", {}).get("code", "unknown")
                    self.stop.set()
        await self.guarded(work())

    async def play_output(self, rate):
        async def work():
            period = 0.01
            deadline = time.monotonic()
            while True:
                packet = self.playback.pop(rate // 100 * 2)
                if packet:
                    _, pcm = packet
                    self.service.transport.send(1, pcm)
                    self.recording.write("agent", pcm, self.seconds())
                    self.result["audio_bytes"]["agent"] += len(pcm)
                deadline += period
                now = time.monotonic()
                if deadline < now:
                    deadline = now + period
                await asyncio.sleep(max(0, deadline - now))
        await self.guarded(work())

    async def monitor(self):
        while not self.stop.is_set():
            state = self.service.state
            if not state.connected:
                self.error = "phone_or_usb_disconnected"
                return
            if not state.active:
                return
            if state.rate != self.result["rate"]:
                self.error = "audio_rate_changed"
                return
            if not state.audio:
                self.error = "phone_audio_disconnected"
                return
            if self.seconds() >= self.limit:
                self.result["end_reason"] = "time_limit"
                return
            if self.finish_after_playback:
                if self.playback.bytes_queued:
                    self.finish_empty_since = None
                elif self.finish_empty_since is None:
                    self.finish_empty_since = time.monotonic()
                elif time.monotonic() - self.finish_empty_since >= 0.3:
                    self.result["end_reason"] = "agent_finished"
                    return
            await asyncio.sleep(0.1)

    async def ensure_hangup(self):
        try:
            # Resolve command ownership before touching a call that may belong to the user.
            response_deadline = time.monotonic() + 2
            while not (self.dial_accepted or self.dial_rejected) and time.monotonic() < response_deadline:
                await asyncio.sleep(0.05)
            if self.dial_rejected:
                return
            if not self.dial_accepted:
                self.result["hangup_warning"] = "拨号接受状态未知，请检查 iPhone"
                return
            self.service.transport.control("flush")
            self.service.transport.control("hangup")
            sent_at = time.monotonic()
            last_request = sent_at
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                await self.service.refresh()
                now = time.monotonic()
                if (self.service.state.active or self.service.state.setup) and now - last_request >= 0.5:
                    self.service.transport.control("hangup")
                    last_request = now
                if (now - sent_at >= 1 and not self.service.state.active and
                        self.service.state.setup == 0 and self.service.state.connected):
                    self.result["hangup_confirmed"] = True
                    return
                await asyncio.sleep(0.2)
        except Exception:
            pass
        self.result["hangup_warning"] = "未确认手机已挂断，请检查 iPhone"
