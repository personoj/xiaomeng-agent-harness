import asyncio
import time

import pytest

from xiaoyun.harness import Harness, Response
from xiaoyun.config import Settings
from xiaoyun.memory import SessionStore
from test_harness import FakeProvider


async def ready(tmp_path):
    h=Harness(Settings(data_dir=tmp_path),SessionStore(tmp_path),FakeProvider)
    events=[]
    async def sink(e):events.append(e)
    connection=await h.attach('duplex',sink)
    await h.handle(connection,{'type':'context.update','page_id':'demo','context':{}})
    await h.start()
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
        h.responses['r1'].played_samples=2400
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
