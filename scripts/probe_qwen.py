"""Read-only connection probe; never prints credentials or auth headers."""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiaoyun.config import Settings
from websockets.asyncio.client import connect


async def main():
    cfg = Settings.load()
    url = cfg.endpoint + "?" + urlencode({"model": cfg.model})
    try:
        async with connect(url, additional_headers={"Authorization": "Bearer " + cfg.key()}, open_timeout=20) as ws:
            for _ in range(2):
                message = json.loads(await asyncio.wait_for(ws.recv(), 20))
                print(json.dumps({"type": message.get("type"), "error": message.get("error"), "model": message.get("session", {}).get("model")}, ensure_ascii=False).replace(cfg.key(), "[REDACTED]"))
                if message.get("type") == "session.created":
                    await ws.send(json.dumps({"type": "session.update", "session": {"modalities": ["text", "audio"], "voice": cfg.voice, "turn_detection": {"type": cfg.turn_detection}}}))
                else:
                    break
    except Exception as exc:
        print(json.dumps({"exception": type(exc).__name__, "status": getattr(getattr(exc, "response", None), "status_code", None)}, ensure_ascii=False))
        raise SystemExit(1)


asyncio.run(main())
