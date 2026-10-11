"""Opt-in terminal authentication and model-directed standby control."""
import asyncio
import hmac
import json
import uuid
from pathlib import Path
from .tools import Tool


def authenticate_pairing(path: Path, authorization: str | None):
    try:
        pairing = json.loads(path.read_text(encoding='utf-8'))
        token = pairing['token']
        if not isinstance(token, str) or len(token) < 32:
            return None
        expected = 'Bearer ' + token
        if not hmac.compare_digest((authorization or '').encode(), expected.encode()):
            return None
        return pairing['session_id']
    except (OSError, ValueError, KeyError, TypeError):
        return None


class DeviceGate:
    def __init__(self, session, sink):
        self.session, self.sink = session, sink
        self.awake = False
        self.stop_task = None
        self.request_id = None
        self.standby_ack = False
        # Idempotent channel control does not queue behind page mutations.
        # Apply it on tool.completed, after the call is durably completed, so
        # stopping the session cannot cancel its own running tool call.
        self.end_tool = Tool(
            'end_conversation',
            '结束当前设备语音对话并恢复本地唤醒待机。用户明确告别或要求结束、退下、休息时立即调用，'
            '例如“拜拜”“再见”“你可以退下了”“今天先聊到这里”。不要仅口头答应。'
            '设备会本地播放退出确认，无需生成告别语音。用户否定退出、引用或讨论结束语时不要调用。',
            {'type': 'object', 'properties': {}, 'additionalProperties': False},
            handler=self._end_conversation,
        )

    async def _end_conversation(self, arguments):
        if not self.awake:
            return {'standby_requested': False}
        if self.request_id is None:
            self.request_id = uuid.uuid4().hex
        return {'standby_requested': True, 'request_id': self.request_id}

    async def emit(self, event):
        if (event.get('type') == 'tool.completed' and event.get('name') == self.end_tool.name
                and event.get('status') == 'succeeded' and event.get('result', {}).get('standby_requested')
                and event['result'].get('request_id') == self.request_id
                and self.awake):
            self.awake = False
            self.standby_ack = False
            await self.sink({'type': 'device.standby', 'request_id': self.request_id,
                             'reason': 'spoken_exit', 'session_id': self.session.store.id})
            # Harness emits while holding its lock; stop after that event releases it.
            self.stop_task = asyncio.create_task(self.session.stop())
        if event.get('type') in ('audio.delta', 'audio.end') and not self.awake:
            return
        await self.sink(event)

    async def handle(self, connection, event):
        kind = event.get('type')
        if kind == 'device.standby.ack':
            if event.get('request_id') == self.request_id and self.request_id:
                self.standby_ack = True
            return
        if kind == 'session.start':
            if self.stop_task:
                await self.stop_task
                self.stop_task = None
            if self.end_tool.name in self.session.page_tools:
                raise ValueError('device_tool_name_conflict')
            self.session.register(self.end_tool)
            self.request_id = None
            self.standby_ack = False
            self.awake = True
        elif kind == 'session.stop':
            self.awake = False
        elif kind in ('input.begin', 'input.audio', 'input.text', 'input.commit', 'interrupt') and not self.awake:
            return
        await self.session.handle(connection, event)

    async def close(self):
        self.awake = False
        if self.stop_task:
            await asyncio.gather(self.stop_task, return_exceptions=True)
        if self.session.local_tools.get(self.end_tool.name) is self.end_tool:
            del self.session.local_tools[self.end_tool.name]


class LocalRoutesMiddleware:
    """LAN exposure is restricted to authenticated /device routes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            host = (scope.get('client') or ('',))[0]
            local = host in ('127.0.0.1', '::1', 'testclient', 'testserver')
            if not local and not scope.get('path', '').startswith('/device/'):
                if scope['type'] == 'websocket':
                    await send({'type': 'websocket.close', 'code': 1008})
                else:
                    await send({'type': 'http.response.start', 'status': 403, 'headers': []})
                    await send({'type': 'http.response.body', 'body': b'local_endpoint'})
                return
        await self.app(scope, receive, send)
