import re
from dataclasses import dataclass


@dataclass
class DeviceState:
    conn: int = 0
    audio_state: int = 0
    call: int = 0
    setup: int = 0
    rate: int = 8000
    firmware: int = 0
    transport_lost: bool = False

    @property
    def connected(self):
        return self.conn == 3 and not self.transport_lost

    @property
    def active(self):
        return self.call == 1

    @property
    def audio(self):
        return self.audio_state in (2, 3)

    def update(self, message: str):
        if message.startswith("state:"):
            for key, value in re.findall(r"(conn|audio|call|setup|rate|firmware)=(\d+)", message):
                setattr(self, "audio_state" if key == "audio" else key, int(value))
        elif message == "evt:slc_connected":
            self.conn = 3
        elif message in ("evt:disconnected", "evt:transport_lost"):
            self.conn = self.audio_state = self.call = self.setup = 0
            self.transport_lost = message == "evt:transport_lost"
        elif message.startswith("evt:audio_connected"):
            self.rate = 16000 if "rate=16000" in message else 8000
            self.audio_state = 3 if self.rate == 16000 else 2
        elif message == "evt:audio_disconnected":
            self.audio_state = 0
        elif message == "evt:call_active":
            self.call = 1
        elif message == "evt:call_idle":
            self.call = 0
        elif message.startswith("evt:setup="):
            self.setup = int(message.split("=", 1)[1])
