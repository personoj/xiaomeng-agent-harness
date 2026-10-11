"""Real smart_turn/VAD barge-in check using a synthetic PCM speech fixture."""
import asyncio
from array import array
import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from xiaoyun.config import ROOT, Settings
from xiaoyun.memory import SessionStore
from xiaoyun.harness import Harness
from xiaoyun.provider import QwenProvider


async def interruption_fixture(cfg):
    # Only the test fixture uses TTS; the application remains native speech-to-speech.
    import httpx
    from urllib.parse import urlsplit
    path = ROOT / ".local" / "fixtures" / "interrupt-shanghai.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                headers={"Authorization": "Bearer " + cfg.key()},
                json={"model": "qwen3-tts-flash", "input": {"text": "停止介绍，只回答上海。", "voice": "Cherry", "language_type": "Chinese"}},
            )
            if response.status_code != 200:
                raise RuntimeError("test_fixture_tts_failed: " + str(response.status_code))
            url = response.json()["output"]["audio"]["url"]
            if not (urlsplit(url).hostname or "").endswith(".aliyuncs.com"):
                raise RuntimeError("unexpected_audio_host")
            audio = await client.get(url.replace("http://", "https://", 1))
            audio.raise_for_status()
            path.write_bytes(audio.content)
    return path


async def main():
    fixture = await interruption_fixture(Settings.load())
    with wave.open(str(fixture), "rb") as wav:
        samples = array("h", wav.readframes(wav.getnframes()))
    pcm = array("h", (samples[min(int(i * 1.5), len(samples)-1)] for i in range(int(len(samples)/1.5)))).tobytes()
    output = ROOT / ".local" / "live-checks" / (str(int(time.time())) + "-stream")
    output.mkdir(parents=True)
    cfg = replace(Settings.load(), data_dir=output / "sessions", turn_detection="smart_turn")
    events, protocol = [], []

    class ObservedProvider(QwenProvider):
        async def events(self):
            async for event in super().events():
                record = {k: event[k] for k in ("type", "response_id", "transcript", "error") if k in event}
                if "response" in event:
                    record["status"] = event["response"].get("status")
                protocol.append(json.loads(json.dumps(record).replace(cfg.key(), "[REDACTED]")))
                yield event

    h = Harness(cfg, SessionStore(cfg.data_dir), ObservedProvider)
    async def sink(event): events.append(event)
    async def wait_for(predicate, timeout=35):
        async def polling():
            while True:
                for event in events:
                    if event["type"] == "error": raise RuntimeError(str(event))
                    if predicate(event): return event
                await asyncio.sleep(.02)
        return await asyncio.wait_for(polling(), timeout)

    report = {"model": cfg.model, "mode": cfg.turn_detection, "synthetic_audio": True}
    try:
        conn = await h.attach("audio", sink)
        await h.start()
        await h.handle(conn, {"type": "input.text", "text": "请详细介绍实时语音助手的五个组成部分，每部分用三句话，方便我测试播放中插话。"})
        first = await wait_for(lambda e: e["type"] == "audio.delta")
        rid = first["response_id"]
        # Stream speech at microphone speed, followed by silence for semantic end detection.
        pcm += b"\0\0" * 32000
        for offset in range(0, len(pcm), 640):
            await h.handle(conn, {"type": "input.audio", "audio": base64.b64encode(pcm[offset:offset+640]).decode()})
            await asyncio.sleep(.02)
        speech = await wait_for(lambda e: e["type"] == "turn.started" and e["reason"] == "speech")
        transcript = await wait_for(lambda e: e["type"] == "transcript.user" and e["turn"] >= speech["turn"])
        reply = await wait_for(lambda e: e["type"] == "response.generated" and e["turn"] >= speech["turn"] and bool(e.get("text")))
        clear_at = next(i for i,e in enumerate(events) if e["type"] == "audio.clear" and rid in e["response_ids"])
        assert not any(e["type"] == "audio.delta" and e["response_id"] == rid for e in events[clear_at+1:])
        assert "上海" in reply["text"] and len(reply["text"]) < 40, "new_spoken_intent_not_followed"
        report.update(passed=True, vad_barge_in=True, old_audio_suppressed=True, transcript=transcript["text"], reply=reply["text"])
    except Exception as exc:
        report.update(passed=False, failure=(type(exc).__name__ + ": " + str(exc)).replace(cfg.key(), "[REDACTED]"), recent_protocol=protocol[-12:])
    finally:
        await h.stop()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output / "report.json"), **report}, ensure_ascii=False))
    if not report["passed"]: raise SystemExit(1)


asyncio.run(main())
