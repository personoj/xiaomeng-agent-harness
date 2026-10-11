import json

from fastapi.testclient import TestClient

from xiaoyun.app import create_app
from xiaoyun.config import Settings
from xiaoyun.memory import SessionStore


def settings(tmp_path):
    return Settings(data_dir=tmp_path/'sessions',device_pairing_file=tmp_path/'pairing.json')


def test_catalog_preserves_files_and_does_not_expose_transcripts_tokens_or_calls(tmp_path):
    cfg=settings(tmp_path)
    store=SessionStore(cfg.data_dir)
    store.data['calls']['unfinished']={'status':'dispatched'}
    store.history('user','private transcript')
    cfg.device_pairing_file.write_text(json.dumps({'session_id':store.id,'token':'secret-device-token'*3}))
    before=store.path.read_bytes()
    with TestClient(create_app(cfg)) as client:
        result=client.get('/api/sessions').json()
        row=result['sessions'][0]
        assert row['is_device'] and row['session_id']==store.id
        assert result['device_session_id']==store.id
        assert 'secret-device-token' not in json.dumps(result)
        assert 'private transcript' not in json.dumps(result)
        assert 'unfinished' not in json.dumps(result)
        assert store.path.read_bytes()==before
        assert not client.app.state.sessions


def test_named_new_session_is_durable_and_switch_preserves_old_session_and_token(tmp_path):
    cfg=settings(tmp_path)
    with TestClient(create_app(cfg)) as client:
        old=client.post('/api/sessions',json={'name':'原会话'}).json()['session_id']
        new=client.post('/api/sessions',json={'name':'演示新会话'}).json()['session_id']
        cfg.device_pairing_file.write_text(json.dumps({'session_id':old,'token':'same-secret-token'*3}))
        reply=client.post('/api/device/session',json={'session_id':new})
        assert reply.status_code==200 and reply.json()['wake_required']
        pairing=json.loads(cfg.device_pairing_file.read_text())
        assert pairing=={'session_id':new,'token':'same-secret-token'*3}
        rows=client.get('/api/sessions').json()['sessions']
        assert rows[0]['session_id']==new and rows[0]['name']=='演示新会话'
        assert (cfg.data_dir/(old+'.json')).exists()
        assert client.post('/api/device/session',json={'session_id':new}).json()['changed'] is False
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/sessions').json()['sessions'][0]['name']=='演示新会话'
        assert client.post('/api/device/session',json={'session_id':'f'*32}).status_code==404
        assert client.post('/api/device/session',json={'session_id':'../bad'}).status_code==400
        assert client.get('/api/sessions',headers={'origin':'https://evil.test'}).status_code==403
        assert client.post('/api/device/session',json={'session_id':old},headers={'origin':'https://evil.test'}).status_code==403


def test_device_next_connection_uses_selected_session_with_same_credential(tmp_path):
    cfg=settings(tmp_path)
    token='same-secret-token'*3
    with TestClient(create_app(cfg)) as client:
        old=client.post('/api/sessions',json={}).json()['session_id']
        new=client.post('/api/sessions',json={}).json()['session_id']
        cfg.device_pairing_file.write_text(json.dumps({'session_id':old,'token':token}))
        with client.websocket_connect('/device/ws',headers={'authorization':'Bearer '+token}) as device:
            assert device.receive_json()['session_id']==old
            assert client.post('/api/device/session',json={'session_id':new}).status_code==200
            while True:
                event=device.receive_json()
                if event['type']=='device.standby':
                    assert event['reason']=='session_changed'
                    break
        with client.websocket_connect('/device/ws',headers={'authorization':'Bearer '+token}) as device:
            assert device.receive_json()['session_id']==new


def test_switch_rejects_inflight_operations_and_occupied_audio(tmp_path):
    cfg=settings(tmp_path)
    with TestClient(create_app(cfg)) as client:
        old=client.post('/api/sessions',json={}).json()['session_id']
        new=client.post('/api/sessions',json={}).json()['session_id']
        cfg.device_pairing_file.write_text(json.dumps({'session_id':old,'token':'same-token'*5}))
        client.app.state.sessions[old].store.data['calls']['pending']={'status':'dispatched'}
        assert client.post('/api/device/session',json={'session_id':new}).json()['detail']=='device_operation_in_progress'
        client.app.state.sessions[old].store.data['calls'].clear()
        with client.websocket_connect(f'/ws/{new}?role=audio') as audio:
            assert audio.receive_json()['type']=='attached'
            assert client.post('/api/device/session',json={'session_id':new}).json()['detail']=='target_audio_in_use'
        assert json.loads(cfg.device_pairing_file.read_text())['session_id']==old
