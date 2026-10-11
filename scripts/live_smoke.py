"""Opt-in real API checks. Uses a synthetic audio sink, not a human microphone test."""
import asyncio
from array import array
import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import wave

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiaoyun.config import ROOT, Settings
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from xiaoyun.provider import QwenProvider


async def main():
    out = ROOT / ".local" / "live-checks" / str(int(time.time()))
    out.mkdir(parents=True)
    cfg = replace(Settings.load(), data_dir=out / "sessions", turn_detection="manual")
    protocol = []

    class ObservedProvider(QwenProvider):
        async def events(self):
            async for event in super().events():
                item = {k: event[k] for k in ("type", "response_id", "call_id", "name", "transcript", "text", "error") if k in event}
                if "response" in event:
                    item["response"] = {k: event["response"].get(k) for k in ("id", "status", "status_details")}
                # Never retain provider credential echoes, even on errors.
                item = json.loads(json.dumps(item, ensure_ascii=False).replace(cfg.key(), "[REDACTED]"))
                protocol.append(item)
                yield event

    h = Harness(cfg, SessionStore(cfg.data_dir), ObservedProvider)
    events, audio_buffers, tasks = [], {}, []
    connection = None

    async def sink(event):
        events.append(event)
        kind = event["type"]
        if kind == "audio.delta":
            audio_buffers.setdefault(event["response_id"], bytearray()).extend(base64.b64decode(event["audio"]))
        elif kind == "audio.end":
            tasks.append(asyncio.create_task(h.handle(connection, {"type": "playback.progress", "response_id": event["response_id"], "played_samples": event["samples"], "drained": True})))
        elif kind == "tool.request":
            tasks.append(asyncio.create_task(h.handle(connection, {"type": "tool.result", "call_id": event["call_id"], "result": {"ok": True, "region": event["args"]["region"]}, "context": {"region": event["args"]["region"], "page": "live-test"}})))

    async def wait_for(predicate, start=0, timeout=35):
        async def polling():
            while True:
                for event in events[start:]:
                    if event["type"] == "error":
                        raise RuntimeError(str(event))
                    if predicate(event):
                        return event
                await asyncio.sleep(.02)
        return await asyncio.wait_for(polling(), timeout)

    async def say(text):
        start = len(events)
        await h.handle(connection, {"type": "input.text", "text": text})
        event = await wait_for(lambda e: e["type"] == "response.generated" and bool(e.get("text")) and bool(audio_buffers.get(e["response_id"])), start)
        await asyncio.sleep(.1)
        print(json.dumps({"test": text[:30], "reply": event["text"][:200], "audio_bytes": len(audio_buffers[event["response_id"]])}, ensure_ascii=False), flush=True)
        return event

    report = {"model": cfg.model, "started": time.time(), "synthetic_audio": True, "checks": {}}
    try:
        connection = await h.attach("duplex", sink)
        await h.handle(connection, {"type": "tools.register", "tools": [{"name": "set_test_region", "description": "切换测试页面区域筛选。", "side_effect": True, "parameters": {"type": "object", "properties": {"region": {"type": "string", "enum": ["全部", "华东", "华北"]}}, "required": ["region"], "additionalProperties": False}}]})
        await h.handle(connection, {"type": "context.update", "page_id": "live-test", "context": {"region": "全部", "page": "live-test"}})
        await h.start()
        report["checks"]["connection"] = True
        first = await say("请只说：语音连接测试成功。")
        fixture = bytes(audio_buffers[first["response_id"]])
        with wave.open(str(out / "reply.wav"), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed")); wav.writeframes(fixture)
        report["checks"]["audio_output"] = len(fixture) > 1000
        await say("请记住，我的演示口令是星河七号。简短确认。")
        remembered = await say("我的演示口令是什么？")
        assert "星河" in remembered["text"], "memory_not_recalled"
        report["checks"]["conversation_memory"] = True
        changed = await say("请调用工具，把测试页面区域切换到华东，然后告诉我结果。")
        assert h.context.get("region") == "华东", "page_tool_did_not_execute"
        report["checks"]["page_tool"] = True
        page = await say("请读取当前页面状态，现在选中哪个区域？")
        assert "华东" in page["text"], "page_context_not_used"
        report["checks"]["page_context"] = True

        # Inject model-generated PCM as a synthetic user audio fixture: 24 kHz -> 16 kHz.
        samples = array("h", fixture)
        pcm16 = array("h", (samples[min(int(i * 1.5), len(samples) - 1)] for i in range(int(len(samples) / 1.5)))).tobytes()
        start = len(events)
        for offset in range(0, len(pcm16), 3200):
            await h.handle(connection, {"type": "input.audio", "audio": base64.b64encode(pcm16[offset:offset + 3200]).decode()})
            await asyncio.sleep(.1)
        await h.handle(connection, {"type": "input.commit"})
        heard = await wait_for(lambda e: e["type"] == "transcript.user", start)
        await wait_for(lambda e: e["type"] == "response.generated" and bool(e.get("text")), start)
        report["checks"]["audio_input"] = bool(heard["text"])
        print(json.dumps({"synthetic_input_transcript": heard["text"]}, ensure_ascii=False), flush=True)

        await asyncio.sleep(.1)
        await h.stop(); await h.start()
        restored = await say("我的演示口令是什么？")
        assert "星河" in restored["text"], "reconnect_memory_not_restored"
        report["checks"]["reconnect_memory"] = True
        start = len(events)
        await h.handle(connection, {"type": "input.text", "text": "请用几段话详细解释实时语音系统的组成。"})
        first_audio = await wait_for(lambda e: e["type"] == "audio.delta", start)
        await h.handle(connection, {"type": "interrupt"})
        cutoff = len(events)
        await asyncio.sleep(2)
        assert not any(e["type"] == "audio.delta" and e["response_id"] == first_audio["response_id"] for e in events[cutoff:]), "old_audio_after_interrupt"
        report["checks"]["interrupt"] = True
        report["first_audio_ms"] = [e["first_audio_ms"] for e in events if e["type"] == "metrics"]
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["failure"] = str(exc).replace(cfg.key(), "[REDACTED]")
        print(json.dumps({"failure": report["failure"], "recent_protocol": protocol[-12:]}, ensure_ascii=False), flush=True)
    finally:
        await h.stop()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(out / "report.json"), **report}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


asyncio.run(main())
