import asyncio
import base64
import json
import os
from urllib.parse import quote

from websockets.asyncio.client import connect


def session_config(task: str):
    return {
        "type": "session.update",
        "session": {
            "type": "realtime", "output_modalities": ["audio"],
            "instructions": (
                "你通过用户的手机进行真实电话沟通。使用简洁自然的中文。"
                "开场说明你是用户委托的 AI 助手，并说明通话会录音用于总结；"
                "如果对方不愿继续或不愿录音，礼貌结束。"
                "只完成以下委托，不自行拨打其他号码，不作超出委托的承诺。"
                "对方说话时停止讲话，电话结束时调用 end_call。委托内容：" + task
            ),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")},
                    "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                       "silence_duration_ms": 600,
                                       "create_response": True, "interrupt_response": True},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000},
                           "voice": os.getenv("OPENAI_VOICE", "marin")},
            },
            "tools": [{"type": "function", "name": "end_call",
                       "description": "电话委托已完成或对方希望结束，结束通话。",
                       "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}],
        },
    }


class Realtime:
    input_rate = 24000
    output_rate = 24000

    def __init__(self, task: str, key: str):
        self.task, self.key = task, key
        self.socket = None
        self.context = None

    async def open(self):
        model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
        self.context = connect("wss://api.openai.com/v1/realtime?model=" + quote(model, safe=""),
                               additional_headers={"Authorization": "Bearer " + self.key},
                               open_timeout=15, max_size=4 * 1024 * 1024)
        self.socket = await self.context.__aenter__()
        await self.send(session_config(self.task))
        async with asyncio.timeout(15):
            while True:
                event = await self.receive()
                if event["type"] == "session.updated":
                    return
                if event["type"] == "error":
                    raise RuntimeError("OpenAI 拒绝会话配置：" + event.get("error", {}).get("code", "unknown"))

    async def send(self, event):
        await self.socket.send(json.dumps(event))

    async def receive(self):
        return json.loads(await self.socket.recv())

    async def append(self, pcm: bytes):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")})

    async def greet(self):
        await self.send({"type": "response.create", "response": {
            "instructions": "电话已接通。请先简短自我介绍和说明录音，再说明来意。"}})

    async def truncate(self, item: str, sent_ms: int):
        await self.send({"type": "conversation.item.truncate", "item_id": item,
                         "content_index": 0, "audio_end_ms": max(0, sent_ms - 40)})

    async def close(self):
        if self.context and self.socket:
            await self.context.__aexit__(None, None, None)
