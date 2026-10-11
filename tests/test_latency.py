"""Synthetic protocol timing tests; no provider, hardware or acoustic measurements."""
import asyncio
import base64
import time

import pytest

from xiaoyun.config import Settings
from xiaoyun.harness import Harness
from xiaoyun.latency import TurnTimings
from xiaoyun.memory import SessionStore
from test_harness import FakeProvider, audio, call, done, flush, model, page_tool, response


@pytest.fixture
async def ptt_timing(tmp_path):
    h = Harness(Settings(data_dir=tmp_path), SessionStore(tmp_path), FakeProvider)
    events = []
    async def sink(event):
        events.append(event)
    conn = await h.attach('duplex', sink)
    await page_tool(h, conn)
    await h.start('push_to_talk')
    yield h, conn, events
    await h.stop()
    tasks = list(h.tool_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def begin(h, conn):
    await h.handle(conn, {'type': 'input.begin'})
    await h.handle(conn, {'type': 'input.audio',
                          'audio': base64.b64encode(b'\0\0' * 3200).decode()})


def stage(events, name):
    return [e for e in events if e.get('latency', {}).get('stage') == name]


async def test_commit_timing_includes_session_lock_wait(ptt_timing):
    h, conn, events = ptt_timing
    await begin(h, conn)
    async with h.lock:
        task = asyncio.create_task(h.handle(conn, {'type': 'input.commit'}))
        await flush()
        await asyncio.sleep(.015)  # Inject a local lock wait, not network latency.
    await task
    timing = stage(events, 'input_commit_sent')[0]['latency']
    assert timing['origin'] == 'input.commit_received'
    assert timing['clock'] == 'harness_monotonic'
    assert timing['elapsed_ms'] >= 15


async def test_same_turn_asr_tool_acceptance_and_actual_playback_stay_distinct(ptt_timing):
    h, conn, events = ptt_timing
    await begin(h, conn)
    await h.handle(conn, {'type': 'input.commit'})
    turn = h.turn
    await model(h, {'type': 'input_audio_buffer.committed', 'item_id': 'speech'})
    await response(h)
    await call(h, 'set_region', args={'region': 'synthetic'})
    await flush()
    assert not stage(events, 'tool_dispatch_started')
    await asyncio.sleep(.01)  # Tool arguments arrived before final ASR.
    await model(h, {'type': 'conversation.item.input_audio_transcription.completed',
                    'item_id': 'speech', 'transcript': 'synthetic command'})
    await flush()
    request = stage(events, 'tool_dispatch_started')[0]
    assert request['turn'] == turn and request['response_id'] == 'r1'
    assert request['latency']['elapsed_ms'] >= stage(events, 'final_transcript_ready')[0]['latency']['elapsed_ms']
    await h.handle(conn, {'type': 'tool.result', 'call_id': 'c1',
                          'result': {'accepted': True, 'task_id': 'job'}})
    await flush()
    assert stage(events, 'tool_result_recorded')
    assert not stage(events, 'task_result_reported')
    await done(h)
    await response(h, 'r2')
    await audio(h, 'r2')
    await done(h, 'r2')
    assert stage(events, 'first_generated_audio_received')[0]['first_audio_kind'] == 'generated_at_harness'
    assert not stage(events, 'first_playback_progress_received')
    assert not stage(events, 'playback_drain_received')
    await h.handle(conn, {'type': 'task.update', 'task_id': 'job', 'source_call_id': 'c1',
                          'status': 'succeeded', 'result': {'success': True}})
    assert stage(events, 'task_result_reported')
    await asyncio.sleep(.01)  # Playback report is deliberately later than generation.
    await h.handle(conn, {'type': 'playback.progress', 'response_id': 'r2', 'played_samples': 1200})
    await h.handle(conn, {'type': 'playback.progress', 'response_id': 'r2', 'played_samples': 1200})
    assert len(stage(events, 'first_playback_progress_received')) == 1
    assert not stage(events, 'playback_drain_received')
    await h.handle(conn, {'type': 'playback.progress', 'response_id': 'r2',
                          'played_samples': 2400, 'drained': True})
    assert stage(events, 'playback_drain_received')
    timings = [event['latency'] for event in events if 'latency' in event]
    assert {row['trace_id'] for row in timings} == {f'{h.store.id}:{turn}'}
    assert all(row['origin'] == 'input.commit_received' for row in timings)


async def test_late_effect_and_completion_keep_original_turn_correlation(ptt_timing):
    h, conn, events = ptt_timing
    await h.handle(conn, {'type': 'input.text', 'text': 'synthetic command'})
    old_turn = h.turn
    await response(h)
    await call(h, 'set_region', args={'region': 'synthetic'})
    await flush()
    await begin(h, conn)  # Cancellation must not erase an already-dispatched effect.
    assert h.turn != old_turn
    await h.handle(conn, {'type': 'tool.result', 'call_id': 'c1',
                          'result': {'accepted': True, 'task_id': 'old-job'}})
    await flush()
    completed = stage(events, 'tool_result_recorded')[0]
    assert completed['turn'] == old_turn
    assert completed['latency']['trace_id'] == f'{h.store.id}:{old_turn}'
    await h.handle(conn, {'type': 'task.update', 'task_id': 'old-job', 'source_call_id': 'c1',
                          'status': 'succeeded', 'result': {'success': True}})
    task = stage(events, 'task_result_reported')[0]
    assert task['source_turn'] == old_turn
    assert task['latency']['trace_id'] == f'{h.store.id}:{old_turn}'
    assert not any(e['type'] == 'playback.observed' for e in events)


def test_timing_history_is_bounded_and_missing_origins_are_not_invented():
    timings = TurnTimings()
    for turn in range(100):
        timings.start(turn, 'input.commit_received', time.monotonic())
    assert len(timings.origins) == 64
    event = {'type': 'tool.completed', 'session_id': 'synthetic', 'turn': 0}
    assert timings.stamp(event) == event


async def test_task_announcement_keeps_original_sources_through_a_later_turn(ptt_timing):
    h, conn, events = ptt_timing
    h.turn = 5
    h.turn_started_at = time.monotonic() - 1
    for turn in (1, 3, 5):
        h.timings.start(turn, 'input.commit_received', time.monotonic() - 2)
    h.speech_active = True  # Queue two completed jobs from different earlier turns.
    for turn in (1, 3):
        h.store.data['calls'][f'c{turn}'] = {
            'status': 'succeeded', 'page_id': h.page_id, 'turn': turn,
            'result': {'task_id': f'job{turn}'},
        }
        await h.handle(conn, {'type': 'task.update', 'task_id': f'job{turn}',
                              'source_call_id': f'c{turn}', 'status': 'succeeded', 'result': {}})
    await flush()
    assert not stage(events, 'task_announcement_requested')
    h.speech_active = False
    await h.emit({'type': 'test.transition'})
    await flush()
    announced = stage(events, 'task_announcement_requested')[0]
    assert [source['source_turn'] for source in announced['task_sources']] == [1, 3]
    await response(h, 'result-speech')
    await audio(h, 'result-speech')
    await done(h, 'result-speech')
    metric = stage(events, 'first_generated_audio_received')[0]
    assert metric['first_audio_origin'] == 'task_announcement_requested'
    assert metric['latency']['turn'] == 5
    await h.handle(conn, {'type': 'playback.progress', 'response_id': 'result-speech',
                          'played_samples': 2400, 'drained': True})
    for event in (metric, stage(events, 'first_playback_progress_received')[0],
                  stage(events, 'playback_drain_received')[0]):
        assert [source['task_id'] for source in event['task_sources']] == ['job1', 'job3']
        assert [source['trace_id'] for source in event['task_sources']] == [
            f'{h.store.id}:1', f'{h.store.id}:3']
        assert all(source['elapsed_ms'] >= 2000 for source in event['task_sources'])
