"""Qwen native realtime transport; no text LLM / TTS cascade."""
import asyncio
import json
import uuid
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from .config import Settings


class QwenProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ws = None
        self.send_lock = asyncio.Lock()

    async def connect(self, instructions: str, tools: list):
        cfg = self.settings
        self.ws = await connect(
            cfg.endpoint + "?" + urlencode({"model": cfg.model}),
            additional_headers={"Authorization": "Bearer " + cfg.key()},
            open_timeout=20, max_size=4 * 1024 * 1024,
            ping_interval=20, ping_timeout=20,
        )
        event = json.loads(await asyncio.wait_for(self.ws.recv(), 20))
        if event.get("type") != "session.created":
            await self.close()
            raise RuntimeError("provider_session_not_created")
        await self.send({"type": "session.update", "session": {
            "modalities": ["text", "audio"], "voice": cfg.voice,
            "instructions": instructions, "tools": tools,
            "input_audio_format": "pcm", "output_audio_format": "pcm",
            "turn_detection": None if cfg.turn_detection == "manual" else {"type": cfg.turn_detection},
        }})
        while True:
            event = json.loads(await asyncio.wait_for(self.ws.recv(), 20))
            if event.get("type") == "error":
                await self.close()
                raise RuntimeError("provider_configuration_rejected")
            if event.get("type") == "session.updated":
                return

    async def send(self, event: dict):
        if self.ws is None:
            raise RuntimeError("provider_disconnected")
        event = {"event_id": uuid.uuid4().hex, **event}
        async with self.send_lock:
            await self.ws.send(json.dumps(event, ensure_ascii=False))

    async def events(self):
        async for raw in self.ws:
            yield json.loads(raw)

    async def close(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            await ws.close()
