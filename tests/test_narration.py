import asyncio
import base64

import pytest

from xiaoyun.config import Settings
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from xiaoyun.narration import AHEAD_SAMPLES, MAX_SAMPLES
from test_harness import FakeProvider, response, call, done, audio, flush


@pytest.fixture
async def connected(tmp_path):
    h = Harness(Settings(data_dir=tmp_path, turn_detection="manual", tool_timeout=1), SessionStore(tmp_path), FakeProvider)
    page_events, audio_events = [], []
    async def page_sink(event): page_events.append(event)
    async def audio_sink(event): audio_events.append(event)
    page = await h.attach("page", page_sink)
    terminal = await h.attach("audio", audio_sink)
    await h.start()
    await h.handle(page, {"type": "tools.register", "tools": [{"name": "explain", "side_effect": False,
                          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]})
    await h.handle(page, {"type": "context.update", "page_id": "lesson", "context": {}})
    await response(h)
    await call(h, "explain")
    await flush()
    yield h, page, terminal, page_events, audio_events
    await h.stop()
    tasks = list(h.tool_tasks.values())
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def prepare(samples=48000, **updates):
    return {"type": "narration.prepare", "source_call_id": "c1", "narration_id": "lesson_1",
            "text": "关节在转，杆长始终不变。", "pcm": base64.b64encode(b"\x00\x10"*samples).decode(), **updates}


async def accept(h, page):
    await h.handle(page, {"type": "tool.result", "call_id": "c1", "result": {"accepted": True, "narration_id": "lesson_1"}})
    await done(h)
    await flush()


async def test_prepare_is_bound_to_page_dispatched_call_turn_and_pcm_limit(connected):
    h, page, terminal, events, _ = connected
    with pytest.raises(ValueError, match="channel_role_mismatch"):
        await h.handle(terminal, prepare())
    await h.handle(page, prepare(source_call_id="foreign"))
    assert events[-1]["type"] == "narration.failed"
    assert events[-1]["code"] == "narration_call_not_current"
    h.store.data["calls"]["c1"]["connection"] = "another-page"
    await h.handle(page, prepare())
    assert events[-1]["code"] == "narration_call_not_current"
    h.store.data["calls"]["c1"]["connection"] = page
    await h.handle(page, prepare(MAX_SAMPLES + 1))
    assert events[-1]["code"] == "invalid_narration_pcm_size"
    await h.handle(page, prepare(pcm="AAAAA"))
    assert events[-1]["type"] == "narration.failed"
    assert h.narration.current is None


async def test_waits_for_real_prefix_drain_and_progress_limits_the_send_window(connected):
    h, page, terminal, events, audio_events = connected
    await audio(h, samples=2400)
    await h.handle(page, prepare())
    await accept(h, page)
    assert not any(e["type"] == "narration.started" for e in events)
    await h.handle(terminal, {"type": "playback.progress", "response_id": "r1", "played_samples": 2400})
    assert not any(e["type"] == "narration.started" for e in events)
    await h.handle(terminal, {"type": "playback.progress", "response_id": "r1", "played_samples": 2400, "drained": True})
    rid = next(e["response_id"] for e in events if e["type"] == "narration.started")
    rec = h.responses[rid]
    assert rec.generated_samples == AHEAD_SAMPLES and not rec.done
    while rec.played_samples < 48000:
        await h.handle(terminal, {"type": "playback.progress", "response_id": rid,
                                 "played_samples": min(rec.generated_samples, rec.played_samples+2400)})
        assert rec.generated_samples-rec.played_samples <= AHEAD_SAMPLES
    assert rec.done and not rec.drained
    assert not any(x["kind"] == "assistant_heard" for x in h.store.data["history"])
    assert not any(e["type"] == "narration.progress" and e["completed"] for e in events)
    await h.handle(terminal, {"type": "playback.progress", "response_id": rid, "played_samples": 48000, "drained": True})
    assert h.store.data["history"][-1]["kind"] == "assistant_heard"
    assert h.narration.current is None and rec.drained
    assert [e for e in events if e["type"] == "narration.progress"][-1]["completed"]
    assert not any(e["type"] == "response.create" for e in h.provider.sent)
    assert not any("pcm" in e or e["type"] == "narration.progress" for e in h.trace)
    assert sum(e["type"] == "audio.end" and e["response_id"] == rid for e in audio_events) == 1


async def test_duplicates_cannot_restart_or_replace_the_recording(connected):
    h, page, terminal, events, _ = connected
    request = prepare(2400)
    await h.handle(page, request)
    await h.handle(page, request)
    assert events[-1]["duplicate"] is True
    await h.handle(page, prepare(2400, text="不同内容"))
    assert events[-1]["code"] == "narration_busy"
    await accept(h, page)
    await h.handle(page, {"type": "tool.result", "call_id": "c1", "result": {"accepted": True, "narration_id": "lesson_1"}})
    await done(h)
    assert sum(e["type"] == "narration.started" for e in events) == 1
    await h.handle(page, request)
    assert events[-1]["code"] == "narration_call_not_current"


async def test_new_button_turn_clears_active_audio_and_never_resumes_old_script(connected):
    h, page, terminal, events, audio_events = connected
    await h.handle(page, prepare())
    await accept(h, page)
    rid = h.narration.current.response_id
    generated = h.responses[rid].generated_samples
    await h.handle(terminal, {"type": "input.begin"})
    assert h.narration.current is None and h.responses[rid].interrupted and h.responses[rid].done
    assert any(e["type"] == "audio.clear" and rid in e["response_ids"] for e in audio_events)
    before = len([e for e in audio_events if e["type"] == "audio.delta"])
    await h.handle(terminal, {"type": "playback.progress", "response_id": rid, "played_samples": generated, "drained": True})
    assert before == len([e for e in audio_events if e["type"] == "audio.delta"])
    assert not any(x["kind"] == "assistant_heard" for x in h.store.data["history"])
    assert h.store.data["history"][-1]["kind"] == "assistant_interrupted"
    # Local narration has no cloud response to cancel or wait for.
    assert not any(e["type"] == "response.cancel" for e in h.provider.sent)


async def test_page_cancel_while_prepared_suppresses_late_tool_continuation(connected):
    h, page, _, events, _ = connected
    await h.handle(page, prepare())
    turn = h.turn
    await h.handle(page, {"type": "narration.cancel", "narration_id": "unrelated"})
    assert h.narration.current
    await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1"})
    await accept(h, page)
    assert h.turn == turn and h.narration.current is None
    assert not any(e["type"] == "narration.started" for e in events)
    assert not any(e["type"] == "response.create" for e in h.provider.sent)


async def test_disconnect_drops_pcm_and_fails_pending_delivery(connected):
    h, page, _, events, audio_events = connected
    await h.handle(page, prepare())
    await accept(h, page)
    rid = h.narration.current.response_id
    await h.detach(page)
    assert h.narration.current is None
    assert h.responses[rid].interrupted
    assert any(e["type"] == "audio.clear" and rid in e["response_ids"] for e in audio_events)
    assert any(e["type"] == "narration.stopped" and e["reason"] == "page_disconnected" for e in events)


async def test_source_cancel_before_prepare_rejects_late_pcm_and_acceptance(connected):
    h, page, _, events, audio_events = connected
    turn = h.turn
    cancellation = {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"}
    await h.handle(page, cancellation)
    await h.handle(page, cancellation)
    assert h.narration.cancelled_calls == {"c1"}
    assert events[-1]["type"] == "narration.stopped"
    assert events[-1]["source_call_id"] == "c1" and events[-1]["response_id"] is None
    await h.handle(page, prepare())
    assert events[-1]["code"] == "narration_call_cancelled"
    await accept(h, page)
    assert h.turn == turn and h.narration.current is None
    assert not any(e["type"] == "narration.started" for e in events)
    assert not any(e["type"] in ("audio.delta", "audio.clear") for e in audio_events)
    assert not any(e["type"] == "response.create" for e in h.provider.sent)


async def test_source_cancel_between_prepare_and_accepted_result(connected):
    h, page, _, events, audio_events = connected
    await h.handle(page, prepare())
    await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"})
    await accept(h, page)
    assert h.narration.current is None and h.narration.cancelled_calls == {"c1"}
    assert not any(e["type"] == "narration.started" for e in events)
    assert not any(e["type"] in ("audio.delta", "audio.clear") for e in audio_events)
    assert not any(e["type"] == "response.create" for e in h.provider.sent)


@pytest.mark.parametrize("invalid", ["connection", "page", "turn", "response", "interrupted", "membership", "status", "unknown"])
async def test_pre_prepare_cancel_requires_a_live_owned_dispatched_source(connected, invalid):
    h, page, _, events, _ = connected
    source = h.store.data["calls"]["c1"]
    if invalid == "connection": source["connection"] = "previous-page-connection"
    if invalid == "page": source["page_id"] = "another-page"
    if invalid == "turn": source["turn"] -= 1
    if invalid == "response": source["response_id"] = "missing"
    if invalid == "interrupted": h.responses["r1"].interrupted = True
    if invalid == "membership": h.responses["r1"].calls.clear()
    if invalid == "status": source["status"] = "succeeded"
    await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1",
                          "source_call_id": "unknown" if invalid == "unknown" else "c1"})
    assert not h.narration.cancelled_calls
    assert not any(e["type"] == "narration.stopped" for e in events)


@pytest.mark.parametrize("fields,code", [
    ({"narration_id": "x" * 129}, "invalid_narration_id"),
    ({"narration_id": []}, "invalid_narration_id"),
    ({"source_call_id": []}, "invalid_narration_call"),
    ({"source_call_id": ""}, "invalid_narration_call"),
    ({"source_call_id": None}, "invalid_narration_call"),
])
async def test_source_cancellation_rejects_invalid_identifiers(connected, fields, code):
    h, page, _, _, _ = connected
    with pytest.raises(ValueError, match=code):
        await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1", **fields})
    assert not h.narration.cancelled_calls


async def test_stale_cancellation_does_not_stop_newer_narration_in_same_response(connected):
    h, page, _, events, audio_events = connected
    await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"})
    await call(h, "explain", cid="c2")
    await flush()
    await h.handle(page, prepare(source_call_id="c2", narration_id="lesson_2"))
    await h.handle(page, {"type": "tool.result", "call_id": "c1", "result": {"error": "cancelled"}})
    await h.handle(page, {"type": "tool.result", "call_id": "c2", "result": {"accepted": True, "narration_id": "lesson_2"}})
    await done(h)
    await flush()
    item = h.narration.current
    assert item and item.id == "lesson_2" and item.started
    before = len(audio_events)
    for nid, call_id in [("lesson_1", "c1"), ("lesson_2", "c1"), ("wrong_id", "c2")]:
        await h.handle(page, {"type": "narration.cancel", "narration_id": nid, "source_call_id": call_id})
    assert h.narration.current is item
    assert not h.responses[item.response_id].interrupted
    assert not any(e["type"] == "audio.clear" for e in audio_events[before:])
    assert h.narration.cancelled_calls == {"c1"}
    assert not any(e["type"] == "response.create" for e in h.provider.sent)


async def test_cancelled_narration_does_not_swallow_another_tools_continuation(connected):
    h, page, _, _, _ = connected
    await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"})
    await call(h, "explain", cid="c2")
    await flush()
    for call_id, result in [("c1", {"error": "cancelled"}), ("c2", {"ok": True})]:
        await h.handle(page, {"type": "tool.result", "call_id": call_id, "result": result})
    await done(h)
    await flush()
    assert sum(e["type"] == "response.create" for e in h.provider.sent) == 1


@pytest.mark.parametrize("transition", ["new_turn", "restart"])
async def test_cancellation_tombstones_expire_with_the_source_scope(connected, transition):
    h, page, terminal, events, _ = connected
    for index in range(100):
        await h.handle(page, {"type": "narration.cancel", "narration_id": f"cancel_{index}", "source_call_id": "c1"})
    assert h.narration.cancelled_calls == {"c1"}  # Store calls, not arbitrary narration IDs.
    if transition == "new_turn":
        await h.handle(terminal, {"type": "input.begin"})
    else:
        await h.stop()
        await h.start()
    assert not h.narration.cancelled_calls
    await h.handle(page, prepare())
    assert events[-1]["code"] == "narration_call_not_current"


async def test_replacement_page_cannot_retire_previous_connection_call(connected):
    h, page, _, _, _ = connected
    await h.detach(page)
    events = []
    async def sink(event): events.append(event)
    replacement = await h.attach("page", sink)
    await h.handle(replacement, {"type": "context.update", "page_id": "lesson", "context": {}})
    await h.handle(replacement, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"})
    assert not h.narration.cancelled_calls
    with pytest.raises(ValueError, match="connection_expired"):
        await h.handle(page, {"type": "narration.cancel", "narration_id": "lesson_1", "source_call_id": "c1"})
