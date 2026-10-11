import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from xiaoyun.app import create_app
from xiaoyun.config import Settings
from xiaoyun.device import DeviceGate
from xiaoyun.harness import Harness
from xiaoyun.memory import SessionStore
from test_harness import FakeProvider, model, response, call, flush


@pytest.mark.asyncio
async def test_model_exit_tool_stops_device_and_rearms_after_next_wake(tmp_path):
    emitted = []
    async def sink(e): emitted.append(e)
    session = Harness(Settings(data_dir=tmp_path), SessionStore(tmp_path), FakeProvider)
    gate = DeviceGate(session, sink)
    connection = await session.attach('audio', gate.emit)
    await gate.handle(connection, {'type': 'session.start'})
    provider = session.provider
    assert any(t['function']['name'] == 'end_conversation' for t in provider.tools)
    # User transcripts alone never exit: there is no keyword fallback.
    for phrase in ('好的退下吧', '拜拜', '再见', '你可以退下了', '今天先聊到这里'):
        await gate.emit({'type': 'transcript.user', 'text': phrase})
    assert gate.awake
    await response(session)
    # Ending the audio channel must not wait for a long-running page mutation.
    await session.mutation_lock.acquire()
    try:
        await call(session, 'end_conversation')
        await asyncio.wait_for(session.tool_tasks['c1'], 1)
    finally:
        session.mutation_lock.release()
    await gate.stop_task
    assert provider.closed and not gate.awake
    assert session.store.data['calls']['c1']['status'] == 'succeeded'
    standby = next(e for e in emitted if e['type'] == 'device.standby')
    assert standby['reason'] == 'spoken_exit'
    await gate.emit({'type': 'audio.delta', 'response_id': 'late'})
    assert not any(e['type'] == 'audio.delta' for e in emitted)
    await gate.handle(connection, {'type': 'input.audio', 'audio': 'AAA='})
    assert not any(e['type'] == 'input_audio_buffer.append' for e in provider.sent)
    await gate.handle(connection, {'type': 'device.standby.ack', 'request_id': gate.request_id})
    assert gate.standby_ack
    completed = next(e for e in emitted if e['type'] == 'tool.completed')
    await gate.emit(completed)
    assert sum(e['type'] == 'device.standby' for e in emitted) == 1
    await gate.handle(connection, {'type': 'session.start'})
    assert gate.awake and session.provider is not provider
    await gate.emit(completed)
    assert gate.awake  # A completed call from the old wake cannot end this one.
    await gate.handle(connection, {'type': 'input.audio', 'audio': 'AAA='})
    assert session.provider.sent[-1]['type'] == 'input_audio_buffer.append'
    await response(session, 'r2')
    await call(session, 'end_conversation', cid='c2', rid='r2')
    await asyncio.wait_for(session.tool_tasks['c2'], 1)
    await gate.stop_task
    assert sum(e['type'] == 'device.standby' for e in emitted) == 2
    await gate.close()
    assert 'end_conversation' not in session.tools()
    await session.detach(connection)


@pytest.mark.asyncio
async def test_exit_tool_invalid_arguments_do_not_stop_device(tmp_path):
    session = Harness(Settings(data_dir=tmp_path), SessionStore(tmp_path), FakeProvider)
    events = []
    async def sink(event): events.append(event)
    gate = DeviceGate(session, sink)
    connection = await session.attach('audio', gate.emit)
    await gate.handle(connection, {'type': 'session.start'})
    await response(session)
    await call(session, 'end_conversation', args={'unsupported': True})
    await flush()
    assert gate.awake and not any(e['type'] == 'device.standby' for e in events)
    await gate.close()
    await session.detach(connection)


def test_lan_routes_require_device_token_and_cannot_read_browser_sessions(tmp_path):
    pairing = tmp_path/'pairing.json'
    pairing.write_text(json.dumps({'token':'a'*48,'session_id':'1234567890abcdef1234567890abcdef'}))
    cfg=Settings(data_dir=tmp_path/'sessions',device_pairing_file=pairing,lan_hosts=['192.168.1.20'])
    with TestClient(create_app(cfg),base_url='http://192.168.1.20',client=('192.168.1.30',40000)) as c:
        assert c.get('/api/sessions/1234567890abcdef1234567890abcdef').status_code == 403
        assert c.post('/api/sessions',json={}).status_code == 403
        assert c.get('/device/health').status_code == 401
        headers={'Authorization':'Bearer '+'a'*48}
        assert c.get('/device/health',headers=headers).json()['session_id']=='1234567890abcdef1234567890abcdef'
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/device/ws'): pass
        with c.websocket_connect('/device/ws',headers=headers) as ws:
            attached=ws.receive_json()
            assert attached['role']=='audio' and attached['session_id']=='1234567890abcdef1234567890abcdef'
            assert ws.receive_json()['type']=='integration.updated'
            ws.send_json({'type':'tools.register','tools':[]})
            assert ws.receive_json()['code']=='channel_role_mismatch'
