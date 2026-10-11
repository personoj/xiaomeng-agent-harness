import asyncio

from test_harness import runtime, model, page_tool, response, call, done, flush, audio
from xiaoyun.tools import Tool


async def speech(h, item="speech1"):
    await model(h, {"type": "input_audio_buffer.speech_started", "item_id": item})
    await model(h, {"type": "input_audio_buffer.speech_stopped", "item_id": item})


async def transcript(h, item="speech1", text="切换到华东"):
    await model(h, {"type": "conversation.item.input_audio_transcription.completed", "item_id": item, "transcript": text})


async def test_audio_page_write_waits_for_same_input_final_transcript_without_blocking_audio(runtime):
    h, conn, events = runtime
    await page_tool(h, conn); await speech(h); await response(h)
    await call(h, "set_region", args={"region": "华东"}); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    await audio(h)
    assert any(e["type"] == "audio.delta" for e in events)
    await transcript(h); await flush()
    assert len([e for e in events if e["type"] == "tool.request"]) == 1
    await h.handle(conn, {"type": "tool.result", "call_id": "c1", "result": {"ok": True}})
    await done(h); await flush()
    assert h.store.data["calls"]["c1"]["status"] == "succeeded"


async def test_missing_transcript_cannot_mutate_page_or_claim_unknown_effect(runtime):
    h, conn, events = runtime
    h.speech_confirmation_timeout = .02
    await page_tool(h, conn); await speech(h); await response(h)
    await call(h, "set_region", args={"region": "华东"}); await done(h)
    await asyncio.sleep(.04); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    assert h.store.data["calls"]["c1"]["status"] == "failed"
    assert h.store.data["calls"]["c1"]["result"]["error"] == "unconfirmed_speech"
    assert any(e["type"] == "tool.blocked" for e in events)


async def test_late_old_transcript_does_not_authorize_a_new_speech_turn(runtime):
    h, conn, events = runtime
    h.speech_confirmation_timeout = .02
    await page_tool(h, conn); await speech(h, "old")
    await speech(h, "new"); await transcript(h, "old")
    # Clear the old pending response by letting its expected response arrive first.
    await response(h, "old-response"); await done(h, "old-response")
    await response(h); await call(h, "set_region", args={"region": "华东"})
    await asyncio.sleep(.04); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    assert h.speech_inputs.turns[h.turn].text == ""
    assert h.store.data["history"][0]["turn"] == h.turn - 1


async def test_invalid_smart_turn_does_not_leave_a_phantom_expected_response(runtime):
    h, conn, events = runtime
    await model(h, {"type": "input_audio_buffer.speech_started", "item_id": "noise"})
    await model(h, {"type": "input_audio_buffer.speech_stopped", "item_id": "noise", "reason": "turn_invalid"})
    assert not h.expected_turns and not h.speech_active
    assert h.state == "listening"
    await speech(h, "real"); await transcript(h, "real"); await response(h)
    assert h.responses["r1"].turn == h.turn and not h.responses["r1"].interrupted


async def test_session_stop_while_waiting_for_transcript_never_dispatches(runtime):
    h, conn, events = runtime
    h.speech_confirmation_timeout = .02
    await page_tool(h, conn); await speech(h); await response(h)
    await call(h, "set_region", args={"region": "华东"}); await done(h); await flush()
    await h.stop(); await transcript(h); await asyncio.sleep(.04); await flush()
    assert not any(e["type"] == "tool.request" for e in events)
    assert h.store.data["calls"]["c1"]["status"] == "cancelled"


async def test_device_local_tool_keeps_native_model_control_without_asr_gate(runtime):
    h, conn, events = runtime
    invoked = []
    async def end(args):
        invoked.append(True)
        return {"standby_requested": True}
    h.register(Tool("end_conversation", "退出", {"type": "object"}, handler=end))
    await speech(h); await response(h); await call(h, "end_conversation"); await flush()
    assert invoked == [True]
