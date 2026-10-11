import base64
import struct

from test_harness import runtime, response, model, done


def pcm(samples=2400):
    return struct.pack('<h', 4096) * samples


async def test_output_meter_requires_playback_and_late_ack_cannot_relight_it(runtime):
    h, conn, events = runtime
    await response(h)
    await model(h, {'type':'response.audio.delta','response_id':'r1','delta':base64.b64encode(pcm()).decode()})
    assert not [e for e in events if e['type']=='audio.activity' and e['channel']=='output']
    await h.handle(conn, {'type':'playback.progress','response_id':'r1','played_samples':960})
    meter=[e for e in events if e['type']=='audio.activity'][-1]
    assert meter['channel']=='output' and meter['level']==.5 and meter['source']=='playback'
    await h.handle(conn, {'type':'interrupt'})
    assert [e for e in events if e['type']=='audio.activity'][-1]['level']==0
    count=len([e for e in events if e['type']=='audio.activity'])
    await h.handle(conn, {'type':'playback.progress','response_id':'r1','played_samples':2400,'drained':True})
    assert len([e for e in events if e['type']=='audio.activity'])==count
    assert not h.responses['r1'].envelope.windows


async def test_capture_is_separate_from_output_and_not_in_trace_or_history(runtime):
    h, conn, events = runtime
    await h.handle(conn, {'type':'input.audio','audio':base64.b64encode(pcm(1280)).decode()})
    activity=[e for e in events if e['type']=='audio.activity']
    assert activity[-1]['channel']=='input' and activity[-1]['level']==.5
    assert not any(e['type']=='audio.activity' for e in h.trace)
    assert h.store.data['history']==[]
    await h.stop()
    assert [e for e in events if e['type']=='audio.activity' and e['channel']=='input'][-1]['level']==0


async def test_playback_drain_and_native_cancel_clear_output(runtime):
    h, conn, events = runtime
    await response(h)
    await model(h, {'type':'response.audio.delta','response_id':'r1','delta':base64.b64encode(pcm()).decode()})
    await done(h)
    await h.handle(conn, {'type':'playback.progress','response_id':'r1','played_samples':2400,'drained':True})
    assert [e for e in events if e['type']=='audio.activity'][-1]['level']==0
    assert not h.responses['r1'].envelope.windows
    await response(h, 'r2')
    await model(h, {'type':'response.audio.delta','response_id':'r2','delta':base64.b64encode(pcm()).decode()})
    await h.handle(conn, {'type':'playback.progress','response_id':'r2','played_samples':960})
    await model(h, {'type':'response.done','response':{'id':'r2','status':'cancelled'}})
    assert [e for e in events if e['type']=='audio.activity'][-1]['level']==0
