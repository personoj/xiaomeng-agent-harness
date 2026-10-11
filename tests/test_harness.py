import asyncio
import base64
import json
from pathlib import Path

import pytest

from xiaoyun.config import Settings
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from xiaoyun.tools import Tool


class FakeProvider:
    def __init__(self, cfg):
        self.sent = []
        self.queue = asyncio.Queue()
        self.closed = False
    async def connect(self, instructions, tools):
        self.instructions = instructions
        self.tools = tools
    async def send(self, event):
        self.sent.append(event)
    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event
    async def close(self):
        self.closed = True


@pytest.fixture
async def runtime(tmp_path):
    cfg = Settings(data_dir=tmp_path, tool_timeout=.1)
    h = Harness(cfg, SessionStore(tmp_path), FakeProvider)
    events = []
    async def sink(event): events.append(event)
    connection = await h.attach("duplex", sink)
    await h.start()
    yield h, connection, events
    await h.stop()
    tasks = list(h.tool_tasks.values())
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def model(h, event):
    async with h.lock:
        await h._model(event)


async def flush():
    for _ in range(10): await asyncio.sleep(0)


async def response(h, rid="r1"):
    await model(h, {"type": "response.created", "response": {"id": rid}})


async def call(h, name, cid="c1", rid="r1", args=None):
    await model(h, {"type": "response.function_call_arguments.done", "response_id": rid, "call_id": cid, "name": name, "arguments": json.dumps(args or {})})


async def done(h, rid="r1"):
    await model(h, {"type": "response.done", "response": {"id": rid, "status": "completed"}})


async def audio(h, rid="r1", samples=2400):
    await model(h, {"type": "response.audio.delta", "response_id": rid, "delta": base64.b64encode(b"\0\0" * samples).decode()})


async def page_tool(h, conn):
    await h.handle(conn, {"type": "tools.register", "tools": [{"name": "set_region", "description": "切换区域", "side_effect": True, "parameters": {"type": "object", "properties": {"region": {"type": "string"}}, "required": ["region"], "additionalProperties": False}}]})
    await h.handle(conn, {"type": "context.update", "context": {"region": "全部"}})


async def test_generation_is_not_delivery(runtime):
    h, conn, events = runtime
    await response(h)
    await model(h, {"type": "response.audio_transcript.delta", "response_id": "r1", "delta": "你好"})
    await audio(h)
    await done(h)
    assert not h.store.data["history"]
    assert h.state == "speaking"
    await h.handle(conn, {"type": "playback.progress", "response_id": "r1", "played_samples": 2400, "drained": True})
    assert h.store.data["history"][-1]["kind"] == "assistant_heard"
    assert h.state == "listening"


async def test_barge_in_drops_late_audio_and_unheard_text(runtime):
    h, conn, events = runtime
    await response(h)
    await model(h, {"type": "response.output_item.added", "response_id": "r1", "item": {"type": "message", "id": "item1"}})
    await model(h, {"type": "response.audio_transcript.delta", "response_id": "r1", "delta": "还没有播完的内容"})
    await audio(h)
    await h.handle(conn, {"type": "playback.progress", "response_id": "r1", "played_samples": 1200})
    await model(h, {"type": "input_audio_buffer.speech_started"})
    before = len([e for e in events if e["type"] == "audio.delta"])
    await audio(h)
    await done(h)
    assert len([e for e in events if e["type"] == "audio.delta"]) == before
    assert "还没有播完" not in h.store.projection(16000)
    assert h.store.data["history"][-1]["played_ms"] == 50
    assert any(e["type"] == "conversation.item.delete" for e in h.provider.sent)


async def test_interrupt_after_generation_stops_buffered_playback(runtime):
    h, conn, events = runtime
    await response(h); await audio(h); await done(h)
    await h.handle(conn, {"type": "interrupt"})
    assert any(e["type"] == "audio.clear" for e in events)
    await h.handle(conn, {"type": "playback.progress", "response_id": "r1", "played_samples": 2400, "drained": True})
    assert not any(x["kind"] == "assistant_heard" for x in h.store.data["history"])


async def test_batch_waits_for_generation_and_all_tool_results(runtime):
    h, conn, events = runtime
    gates = [asyncio.Future(), asyncio.Future()]
    async def first(args): return await gates[0]
    async def second(args): return await gates[1]
    h.register(Tool("first", "a", {"type": "object"}, handler=first))
    h.register(Tool("second", "b", {"type": "object"}, handler=second))
    await response(h); await call(h, "first"); await call(h, "second", cid="c2")
    await flush(); gates[0].set_result({"a": 1}); await flush()
    assert not any(e["type"] == "response.create" for e in h.provider.sent)
    await done(h)
    assert not any(e["type"] == "response.create" for e in h.provider.sent)
    gates[1].set_result({"b": 2}); await flush(); await done(h)
    assert len([e for e in h.provider.sent if e["type"] == "response.create"]) == 1


async def test_duplicate_call_executes_once(runtime):
    h, conn, events = runtime
    count = 0
    async def execute(args):
        nonlocal count
        count += 1
        return {"ok": True}
    h.register(Tool("once", "a", {"type": "object"}, handler=execute))
    await response(h); await call(h, "once"); await call(h, "once")
    await done(h); await flush()
    assert count == 1
    assert len([e for e in h.provider.sent if e.get("item", {}).get("type") == "function_call_output"]) == 1


async def test_invalid_arguments_do_not_execute(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h); await call(h, "set_region", args={"wrong": 1})
    await done(h); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    assert h.store.data["calls"]["c1"]["status"] == "failed"


async def test_changed_context_rejects_stale_tool(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h)
    await h.handle(conn, {"type": "context.update", "context": {"region": "华北"}})
    await call(h, "set_region", args={"region": "华东"}); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    assert h.store.data["calls"]["c1"]["result"]["error"] == "page_context_changed"


async def test_slow_tool_does_not_block_audio_and_late_write_survives_interruption(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h)
    await call(h, "set_region", args={"region": "华东"}); await flush()
    await audio(h)
    assert any(e["type"] == "audio.delta" for e in events)
    await done(h); await h.handle(conn, {"type": "interrupt"})
    await h.handle(conn, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}, "context": {"region": "华东"}})
    await flush()
    assert h.store.data["calls"]["c1"]["status"] == "succeeded"
    assert not any(e["type"] == "response.create" for e in h.provider.sent)


async def test_late_result_does_not_overwrite_newer_page(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h); await call(h, "set_region", args={"region": "华东"}); await flush()
    await h.handle(conn, {"type": "context.update", "context": {"region": "华南"}})
    await h.handle(conn, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}, "context": {"region": "华东"}})
    await flush()
    assert h.context == {"region": "华南"}
    assert h.store.data["calls"]["c1"]["result"]["context_superseded"]


async def test_timed_out_mutation_is_unknown_and_late_ack_reconciles(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h); await call(h, "set_region", args={"region": "华东"}); await flush()
    await asyncio.sleep(.13)
    assert h.store.data["calls"]["c1"]["status"] == "unknown"
    await h.handle(conn, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}})
    assert h.store.data["calls"]["c1"]["status"] == "succeeded"
    assert len([e for e in h.provider.sent if e.get("item", {}).get("type") == "function_call_output"]) == 1


def test_restart_marks_unconfirmed_effect_unknown_and_restores_memory(tmp_path):
    store = SessionStore(tmp_path)
    store.history("user", "我的演示口令是星河")
    store.data["calls"]["pending"] = {"status": "dispatched"}
    store.save()
    restored = SessionStore(tmp_path, store.id)
    assert "星河" in restored.projection(1000)
    assert restored.data["calls"]["pending"]["status"] == "unknown"


async def test_reconnect_injects_memory_not_stale_page(runtime):
    h, conn, events = runtime
    h.store.history("user", "请记住星河")
    await h.stop(); await h.start()
    assert "星河" in h.provider.instructions


async def test_channel_roles_and_exclusive_ownership(tmp_path):
    h = Harness(Settings(data_dir=tmp_path), SessionStore(tmp_path), FakeProvider)
    async def sink(event): pass
    audio_conn = await h.attach("audio", sink)
    page_conn = await h.attach("page", sink)
    with pytest.raises(ValueError, match="channel_already_connected"):
        await h.attach("duplex", sink)
    with pytest.raises(ValueError, match="channel_role_mismatch"):
        await h.handle(audio_conn, {"type": "context.update"})
    with pytest.raises(ValueError, match="channel_role_mismatch"):
        await h.handle(page_conn, {"type": "input.text", "text": "hello"})


async def test_late_response_created_has_original_turn(runtime):
    h, conn, events = runtime
    await h.handle(conn, {"type": "input.text", "text": "旧问题"})
    await h.handle(conn, {"type": "input.text", "text": "新问题"})
    await response(h, "old"); await audio(h, "old")
    assert h.responses["old"].interrupted
    assert not any(e["type"] == "audio.delta" for e in events)


async def test_unknown_write_cannot_be_retried_with_new_call_id(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h); await call(h, "set_region", args={"region": "华东"}); await flush()
    await asyncio.sleep(.13)
    await response(h, "r2"); await call(h, "set_region", cid="c2", rid="r2", args={"region": "华东"}); await flush()
    assert len([e for e in events if e["type"] == "tool.request"]) == 1
    assert h.store.data["calls"]["c2"]["result"]["error"] == "previous_effect_unconfirmed"


async def test_mutations_are_serial_and_revalidate_after_first_result(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await response(h)
    await call(h, "set_region", args={"region": "华东"})
    await call(h, "set_region", cid="c2", args={"region": "华北"}); await flush()
    assert len([e for e in events if e["type"] == "tool.request"]) == 1
    await h.handle(conn, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}, "context": {"region": "华东"}})
    await flush()
    assert h.store.data["calls"]["c2"]["result"]["error"] == "page_context_changed"


async def test_clean_provider_close_marks_disconnected_and_reconnects(runtime):
    h, conn, events = runtime
    provider = h.provider
    await provider.queue.put(None); await flush()
    assert h.provider is None
    assert h.state == "disconnected"
    await h.start()
    assert h.provider is not provider


async def test_final_playback_ack_updates_interruption_progress(runtime):
    h, conn, events = runtime
    await response(h); await audio(h)
    await h.handle(conn, {"type": "interrupt"})
    await h.handle(conn, {"type": "playback.progress", "response_id": "r1", "played_samples": 1440})
    assert h.store.data["history"][-1]["played_ms"] == 60


async def test_page_disconnect_preserves_voice_and_unconfirmed_outcome(runtime):
    h, conn, events = runtime
    await h.detach(conn)
    async def sink(event): events.append(event)
    a = await h.attach("audio", sink)
    p = await h.attach("page", sink)
    await h.start(); await page_tool(h, p); await response(h); await call(h, "set_region", args={"region": "华东"}); await flush()
    await h.detach(p); await flush()
    assert h.provider is not None
    assert h.context == {}
    assert h.store.data["calls"]["c1"]["status"] == "unknown"


async def test_context_updates_deferred_during_speech_until_response_boundary(runtime):
    h, conn, events = runtime
    await model(h, {"type": "input_audio_buffer.speech_started"})
    await h.handle(conn, {"type": "context.update", "context": {"region": "华东"}})
    assert h.context["region"] == "华东"
    assert not any(e["type"] == "session.update" for e in h.provider.sent)
    await model(h, {"type": "input_audio_buffer.speech_stopped"})
    await response(h); await done(h)
    updates = [e for e in h.provider.sent if e["type"] == "session.update"]
    assert len(updates) == 1
    assert "华东" in updates[0]["session"]["instructions"]


async def test_native_cancel_before_speech_started_removes_unheard_answer(runtime):
    h, conn, events = runtime
    await response(h)
    await model(h, {"type": "response.output_item.added", "response_id": "r1", "item": {"type": "message", "id": "unheard"}})
    await audio(h)
    await model(h, {"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    await h.handle(conn, {"type": "context.update", "context": {"region": "华北"}})
    assert h.speech_active
    assert not any(e["type"] == "session.update" for e in h.provider.sent)
    assert any(e.get("item_id") == "unheard" and e["type"] == "conversation.item.delete" for e in h.provider.sent)


async def test_independent_frontend_registers_actions_and_updates_audio_console(tmp_path):
    h = Harness(Settings(data_dir=tmp_path), SessionStore(tmp_path), FakeProvider)
    audio_events, page_events = [], []
    async def audio_sink(e): audio_events.append(e)
    async def page_sink(e): page_events.append(e)
    a = await h.attach("audio", audio_sink)
    assert not h.integration()["page_connected"]
    assert list(h.tools()) == ["get_page_context"]
    p = await h.attach("page", page_sink)
    await h.handle(p, {"type": "tools.register", "tools": [{"name": "open_panel", "description": "打开前端的面板", "parameters": {"type": "object", "properties": {"panel": {"type": "string"}}, "required": ["panel"]}}]})
    await h.handle(p, {"type": "context.update", "page_id": "any-frontend", "context": {"view": "home"}})
    await h.start(); await response(h); await call(h, "open_panel", args={"panel": "details"}); await flush()
    requests = [e for e in page_events if e["type"] == "tool.request"]
    assert len(requests) == 1
    assert not any(e["type"] == "tool.request" for e in audio_events)
    await h.handle(p, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}, "context": {"view": "details"}})
    await done(h); await flush()
    snapshots = [e["integration"] for e in audio_events if e["type"] == "integration.updated"]
    assert snapshots[-1]["page_id"] == "any-frontend"
    assert snapshots[-1]["context"] == {"view": "details"}
    assert snapshots[-1]["tools"][-1]["source"] == "page"
    await h.detach(p)
    assert audio_events[-1]["integration"]["page_connected"] is False
    await h.stop()
