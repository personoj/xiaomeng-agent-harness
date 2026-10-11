import asyncio
import time

import pytest

from xiaoyun.harness import Harness, Response
from xiaoyun.config import Settings
from xiaoyun.memory import SessionStore
from test_harness import FakeProvider, audio, done, flush, model, response


async def ready(tmp_path, *, input_mode=None):
    h=Harness(Settings(data_dir=tmp_path),SessionStore(tmp_path),FakeProvider)
    events=[]
    async def sink(e):events.append(e)
    connection=await h.attach('duplex',sink)
    await h.handle(connection,{'type':'context.update','page_id':'demo','context':{}})
    await h.start(input_mode)
    h.turn_started_at=time.monotonic()-1
    h.store.data['calls']['c1']={'status':'succeeded','page_id':'demo','result':{'task_id':'job1'}}
    event={'type':'task.update','task_id':'job1','source_call_id':'c1','status':'succeeded','result':{'success':True}}
    return h,connection,events,event


async def test_completion_waits_for_actual_playback_and_deduplicates(tmp_path):
    h,connection,events,event=await ready(tmp_path)
    try:
        h.responses['r1']=Response('r1',h.turn,h.revision,generated_samples=2400,played_samples=0,done=True)
        await h.handle(connection,event)
        await asyncio.sleep(.15)
        assert not any(e['type']=='response.create' for e in h.provider.sent)
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'r1',
                                    'played_samples': 2400, 'drained': True})
        await asyncio.sleep(.15)
        assert len([e for e in h.provider.sent if e['type']=='response.create'])==1
        await h.handle(connection,event)
        assert len([e for e in events if e['type']=='task.completed'])==1
        assert len([e for e in h.store.data['history'] if e['kind']=='task_result'])==1
    finally:await h.stop()


async def test_user_speech_takes_priority_and_stop_discards_pending_announcement(tmp_path):
    h,connection,events,event=await ready(tmp_path)
    h.speech_active=True
    await h.handle(connection,event)
    await asyncio.sleep(.15)
    provider=h.provider
    assert not any(e['type']=='response.create' for e in provider.sent)
    await h.stop()
    await h.start()
    await asyncio.sleep(.15)
    assert not any(e['type']=='response.create' for e in h.provider.sent)
    assert 'task_result' in h.store.projection(16000)
    await h.stop()


async def test_unaccepted_or_foreign_page_job_cannot_claim_completion(tmp_path):
    h,connection,events,event=await ready(tmp_path)
    try:
        with pytest.raises(ValueError,match='task_not_accepted'):
            await h.handle(connection,{**event,'task_id':'invented'})
        h.page_id='other'
        with pytest.raises(ValueError,match='task_not_accepted'):
            await h.handle(connection,event)
    finally:await h.stop()


async def test_completion_while_asleep_is_saved_without_starting_cloud(tmp_path):
    h,connection,events,event=await ready(tmp_path)
    await h.stop()
    await h.handle(connection,event)
    assert h.provider is None and h.task_updates.pending==[]
    assert any(e['type']=='task.ack' for e in events)


async def test_playback_receipt_wakes_announcement_without_polling(tmp_path):
    h, connection, events, event = await ready(tmp_path)
    try:
        h.responses['r1'] = Response('r1', h.turn, h.revision,
                                    generated_samples=2400, done=True)
        await h.handle(connection, event)
        await flush()  # Worker has checked quiet and is now waiting for a transition.
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'r1',
                                    'played_samples': 2400, 'drained': False})
        await flush()
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'r1',
                                    'played_samples': 2400, 'drained': True})
        await flush()  # No real-time polling interval is advanced.
        assert len([e for e in h.provider.sent if e['type'] == 'response.create']) == 1
    finally:
        await h.stop()


@pytest.mark.parametrize('interrupt_event', [
    {'type': 'interrupt'},
    {'type': 'input.begin'},
], ids=['browser-interrupt', 'button-begin'])
async def test_full_sample_interruption_releases_notifications_without_false_drain(tmp_path, interrupt_event):
    h, connection, events, task_event = await ready(tmp_path, input_mode='push_to_talk')
    try:
        await h.handle(connection, {'type': 'input.text', 'text': 'synthetic command'})
        await response(h, 'old')
        await model(h, {'type': 'response.audio_transcript.done', 'response_id': 'old',
                        'transcript': 'synthetic interrupted answer'})
        await audio(h, 'old')
        await done(h, 'old')
        # The client has played all PCM while audio.end is still in transit.
        # VoiceClient.clearAudio reports this count, then removes its playback entry.
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'old',
                                    'played_samples': 2400, 'drained': False})
        old = h.responses['old']
        h.turn_started_at = time.monotonic() - 1
        requested = sum(e['type'] == 'response.create' for e in h.provider.sent)
        await h.handle(connection, task_event)
        await flush()
        assert not h.task_updates.quiet() and not old.drained
        assert sum(e['type'] == 'response.create' for e in h.provider.sent) == requested

        await h.handle(connection, interrupt_event)
        assert old.interrupted and not old.drained
        assert any(e['type'] == 'audio.clear' and 'old' in e['response_ids'] for e in events)
        assert h.store.data['calls']['c1']['status'] == 'succeeded'
        assert h.store.data['calls']['c1']['result'] == {'task_id': 'job1'}
        # A delayed drain must not convert cancellation into successful playback.
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'old',
                                    'played_samples': 2400, 'drained': True})
        assert not old.drained
        assert not any(e['type'] == 'playback.completed' and e['response_id'] == 'old' for e in events)
        assert not any(row['kind'] == 'assistant_heard' for row in h.store.data['history'])
        assert any(row['kind'] == 'assistant_interrupted' and row['response_id'] == 'old'
                   for row in h.store.data['history'])

        if interrupt_event['type'] == 'input.begin':
            await flush()
            assert h.input_open and h.task_updates.pending
            assert sum(e['type'] == 'response.create' for e in h.provider.sent) == requested
            await h.handle(connection, {'type': 'input.commit'})  # Release an empty button press.
        # Advance only the guard's origin; use a real duplicate report to wake the worker.
        h.turn_started_at = time.monotonic() - 1
        await h.handle(connection, task_event)
        await asyncio.wait_for(asyncio.shield(h.task_updates.worker), 1)
        assert sum(e['type'] == 'response.create' for e in h.provider.sent) == requested + 1
        assert not h.task_updates.pending

        await response(h, 'announcement')
        await model(h, {'type': 'response.audio_transcript.done', 'response_id': 'announcement',
                        'transcript': 'synthetic task complete'})
        await audio(h, 'announcement')
        await done(h, 'announcement')
        await h.handle(connection, {'type': 'playback.progress', 'response_id': 'announcement',
                                    'played_samples': 2400, 'drained': True})
        assert h.responses['announcement'].drained
        assert any(row['kind'] == 'assistant_heard' and row['response_id'] == 'announcement'
                   for row in h.store.data['history'])
        # Future tasks must also be free of the cancelled response's impossible drain.
        h.store.data['calls']['c2'] = {'status': 'succeeded', 'page_id': 'demo',
                                      'result': {'task_id': 'job2'}}
        h.turn_started_at = time.monotonic() - 1
        await h.handle(connection, {**task_event, 'source_call_id': 'c2', 'task_id': 'job2'})
        await asyncio.wait_for(asyncio.shield(h.task_updates.worker), 1)
        assert sum(e['type'] == 'response.create' for e in h.provider.sent) == requested + 2
        assert not h.task_updates.pending
    finally:
        await h.stop()


async def test_cancelled_response_done_wakes_pending_announcement(tmp_path):
    h, connection, events, event = await ready(tmp_path)
    try:
        h.responses['old'] = Response('old', h.turn, h.revision, interrupted=True)
        await h.handle(connection, event)
        await flush()
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        await model(h, {'type': 'response.done', 'response': {'id': 'old', 'status': 'cancelled'}})
        await flush()
        assert len([e for e in h.provider.sent if e['type'] == 'response.create']) == 1
    finally:
        await h.stop()


async def test_wakeup_preserves_quiet_guard_and_speech_priority(tmp_path):
    h, connection, events, event = await ready(tmp_path)
    try:
        h.turn_started_at = time.monotonic()
        await h.handle(connection, event)
        await flush()
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        h.speech_active = True
        h.turn_started_at -= 1
        await h.emit({'type': 'test.transition'})
        await flush()
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        h.speech_active = False
        await h.emit({'type': 'test.transition'})
        await flush()
        assert len([e for e in h.provider.sent if e['type'] == 'response.create']) == 1
    finally:
        await h.stop()


async def test_quiet_guard_has_one_deadline_even_without_protocol_events(tmp_path):
    h, connection, events, event = await ready(tmp_path)
    try:
        h.turn_started_at = time.monotonic() - .48
        started_at = h.turn_started_at
        await h.handle(connection, event)
        await flush()
        assert not any(e['type'] == 'response.create' for e in h.provider.sent)
        await asyncio.wait_for(asyncio.shield(h.task_updates.worker), .3)
        assert time.monotonic() - started_at >= .5
        assert len([e for e in h.provider.sent if e['type'] == 'response.create']) == 1
    finally:
        await h.stop()
