import base64

import pytest

from xiaoyun.config import Settings
from xiaoyun.device import DeviceGate
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from test_harness import FakeProvider, model, response, audio, done, page_tool, call, flush


class ConfigProvider(FakeProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.cfg = cfg


@pytest.fixture
async def ptt(tmp_path):
    cfg = Settings(data_dir=tmp_path)
    h = Harness(cfg, SessionStore(tmp_path), ConfigProvider)
    events = []
    async def sink(e): events.append(e)
    gate = DeviceGate(h, sink)
    conn = await h.attach('audio', gate.emit)
    await gate.handle(conn, {'type': 'session.start', 'input_mode': 'push_to_talk'})
    yield h, gate, conn, events
    await gate.close()
    await h.detach(conn)


async def frame(gate, conn, samples=3200):
    await gate.handle(conn, {'type': 'input.audio', 'audio': base64.b64encode(b'\0\0' * samples).decode()})


def sent(h, kind):
    return [e for e in h.provider.sent if e['type'] == kind]


async def test_button_audio_only_uploaded_between_begin_and_commit(ptt):
    h, gate, conn, events = ptt
    assert h.provider.cfg.turn_detection == 'manual'
    assert h.cfg.turn_detection == 'smart_turn'  # Settings are shared by all sessions.
    assert next(e for e in events if e['type'] == 'ready')['input_mode'] == 'push_to_talk'
    await frame(gate, conn)
    assert not sent(h, 'input_audio_buffer.append')
    await gate.handle(conn, {'type': 'input.begin'})
    turn = h.turn
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'input.begin'})  # Duplicate button-down must not erase speech.
    assert h.turn == turn and h.input_samples == 3200
    await gate.handle(conn, {'type': 'input.commit'})
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'input.commit'})
    assert len(sent(h, 'input_audio_buffer.append')) == 1
    assert len(sent(h, 'input_audio_buffer.commit')) == 1
    assert len(sent(h, 'response.create')) == 1
    assert not h.input_open and not h.manual_buffered


async def test_empty_or_short_button_press_does_not_invoke_model(ptt):
    h, gate, conn, events = ptt
    for samples in (0, 1000):
        await gate.handle(conn, {'type': 'input.begin'})
        if samples:
            await frame(gate, conn, samples)
        await gate.handle(conn, {'type': 'input.commit'})
    assert not sent(h, 'input_audio_buffer.commit')
    assert not sent(h, 'response.create')
    assert not h.expected_turns and h.state == 'listening'
    assert sum(e['type'] == 'input.rejected' for e in events) == 2


async def test_press_interrupts_playback_and_waits_for_cancel_ack_before_new_inference(ptt):
    h, gate, conn, events = ptt
    await response(h, 'old')
    await audio(h, 'old')
    await gate.handle(conn, {'type': 'input.begin'})
    assert sent(h, 'response.cancel')
    assert any(e['type'] == 'audio.clear' and 'old' in e['response_ids'] for e in events)
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'input.commit'})
    assert sent(h, 'input_audio_buffer.commit') and not sent(h, 'response.create')
    await model(h, {'type': 'response.done', 'response': {'id': 'old', 'status': 'cancelled'}})
    assert len(sent(h, 'response.create')) == 1
    before = sum(e['type'] == 'audio.delta' for e in events)
    await audio(h, 'old')
    assert sum(e['type'] == 'audio.delta' for e in events) == before


async def test_late_commit_ack_is_bound_to_original_button_turn(ptt):
    h, gate, conn, events = ptt
    await gate.handle(conn, {'type': 'input.begin'})
    first = h.turn
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'input.commit'})
    await gate.handle(conn, {'type': 'input.begin'})
    await model(h, {'type': 'input_audio_buffer.committed', 'item_id': 'old-input'})
    await model(h, {'type': 'conversation.item.input_audio_transcription.completed',
                    'item_id': 'old-input', 'transcript': '旧指令'})
    assert h.speech_inputs.items['old-input'].turn == first
    assert h.speech_inputs.turns[h.turn].text == ''
    assert h.store.data['history'][-1]['turn'] == first


async def test_button_input_still_requires_same_turn_transcript_for_page_write(ptt):
    h, gate, conn, events = ptt
    async def sink(e): events.append(e)
    page = await h.attach('page', sink)
    await page_tool(h, page)
    await gate.handle(conn, {'type': 'input.begin'})
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'input.commit'})
    await model(h, {'type': 'input_audio_buffer.committed', 'item_id': 'button1'})
    await response(h)
    await call(h, 'set_region', args={'region': '华东'})
    await flush()
    assert not any(e['type'] == 'tool.request' for e in events)
    await model(h, {'type': 'conversation.item.input_audio_transcription.completed',
                    'item_id': 'button1', 'transcript': '切换到华东'})
    await flush()
    assert any(e['type'] == 'tool.request' for e in events)
    await h.handle(page, {'type': 'tool.result', 'call_id': 'c1', 'result': {'ok': True}})
    await done(h)
    await flush()
    assert h.store.data['calls']['c1']['status'] == 'succeeded'
    await h.detach(page)


async def test_reconnect_requires_new_button_press_and_browser_retains_default(ptt):
    h, gate, conn, events = ptt
    await gate.handle(conn, {'type': 'input.begin'})
    await frame(gate, conn)
    await gate.handle(conn, {'type': 'session.stop'})
    await gate.handle(conn, {'type': 'input.begin'})
    assert not h.input_open
    await gate.handle(conn, {'type': 'session.start', 'input_mode': 'push_to_talk'})
    await gate.handle(conn, {'type': 'input.commit'})
    await frame(gate, conn)
    assert not sent(h, 'input_audio_buffer.append') and not sent(h, 'response.create')
    await gate.handle(conn, {'type': 'session.stop'})
    await h.start()
    assert h.provider.cfg.turn_detection == 'smart_turn'


async def test_task_announcement_waits_while_button_is_held_even_before_first_audio(ptt):
    h, gate, conn, events = ptt
    await gate.handle(conn, {'type': 'input.begin'})
    h.turn_started_at -= 10
    assert not h.task_updates.quiet()
    await gate.handle(conn, {'type': 'input.commit'})
    assert h.task_updates.quiet()
