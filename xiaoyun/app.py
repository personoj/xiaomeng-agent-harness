import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

from .config import ROOT, Settings
from .harness import Harness
from .memory import SessionStore
from .device import DeviceGate, LocalRoutesMiddleware, authenticate_pairing
from .session_catalog import list_sessions, read_pairing


def create_app(cfg: Settings | None = None):
    cfg = cfg or Settings.load()
    sessions: dict[str, Harness] = {}
    pairing_lock = asyncio.Lock()
    device_sockets = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        for session in sessions.values():
            await session.stop()

    app = FastAPI(title="小萌语音开发台", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver", *cfg.lan_hosts])
    app.add_middleware(LocalRoutesMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.allowed_origins,
                       allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?",
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
    app.state.sessions = sessions

    def get_session(sid):
        try:
            if sid not in sessions:
                store = SessionStore(cfg.data_dir, sid)
                sessions[sid] = Harness(cfg, store)
            return sessions[sid]
        except ValueError:
            raise HTTPException(400, "invalid_session_id") from None

    def check_origin(origin):
        # Browser routes stay local. Device routes use separate bearer authentication.
        from urllib.parse import urlsplit
        if origin and origin not in cfg.allowed_origins and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1", "testserver"):
            raise HTTPException(403, "origin_not_allowed")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "service": "xiaoyun-realtime", "model": cfg.model, "voice": cfg.voice, "turn_detection": cfg.turn_detection}

    @app.post("/api/sessions")
    async def session_create(request: Request):
        check_origin(request.headers.get("origin"))
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(400, "invalid_session")
        name = data.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 60:
            raise HTTPException(400, "invalid_session_name")
        sid = data.get("session_id")
        if sid:
            session = get_session(sid)
        else:
            store = SessionStore(cfg.data_dir)
            store.data["name"] = name.strip()
            store.save()
            session = sessions[store.id] = Harness(cfg, store)
        return {"session_id": session.store.id, "name": session.store.data.get("name", ""), "state": session.state, "history_entries": len(session.store.data["history"])}

    @app.get("/api/sessions")
    async def session_list(request: Request):
        check_origin(request.headers.get("origin"))
        return list_sessions(cfg, sessions)

    @app.post("/api/device/session")
    async def device_session_switch(request: Request):
        check_origin(request.headers.get("origin"))
        data = await request.json()
        sid = data.get("session_id") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise HTTPException(400, "invalid_session_id")
        if sid not in sessions and not (cfg.data_dir / (sid + ".json")).exists():
            raise HTTPException(404, "session_not_found")
        async with pairing_lock:
            pairing = read_pairing(cfg.device_pairing_file)
            if not pairing:
                raise HTTPException(409, "device_not_paired")
            old_sid = pairing["session_id"]
            if old_sid == sid:
                return {"session_id": sid, "changed": False, "wake_required": False}
            old = sessions.get(old_sid)
            target = sessions.get(sid)
            if target and "audio" in target.channels:
                raise HTTPException(409, "target_audio_in_use")
            if old and any(c["status"] in ("running", "dispatched") for c in old.store.data["calls"].values()):
                raise HTTPException(409, "device_operation_in_progress")
            # Keep the existing device credential. Only its conversation binding changes.
            pairing["session_id"] = sid
            tmp = cfg.device_pairing_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(pairing, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(cfg.device_pairing_file)
            for ws, (session, gate) in list(device_sockets.items()):
                if session.store.id != old_sid:
                    continue
                gate.awake = False
                gate.request_id = uuid.uuid4().hex
                gate.standby_ack = False
                await session.stop()
                # The bridge/hardware already handles standby and a closed audio socket.
                await gate.sink({"type": "device.standby", "request_id": gate.request_id,
                                 "reason": "session_changed", "session_id": old_sid})
                deadline = asyncio.get_running_loop().time() + 1
                while not gate.standby_ack and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.02)
                await ws.close(code=1000, reason="session_changed")
            return {"session_id": sid, "changed": True, "wake_required": True}

    @app.get("/api/sessions/{sid}")
    async def session_status(sid: str):
        session = get_session(sid)
        return {"session_id": sid, "state": session.state, "turn": session.turn, "revision": session.revision, "context": session.context, "integration": session.integration(), "trace": list(session.trace), "history": session.store.data["history"][-30:]}

    @app.websocket("/ws/{sid}")
    async def socket(ws: WebSocket, sid: str, role: str = "duplex"):
        try:
            check_origin(ws.headers.get("origin"))
            session = get_session(sid)
        except HTTPException:
            await ws.close(1008)
            return
        await serve_socket(ws, session, role)

    @app.get('/device/health')
    async def device_health(request: Request):
        sid = authenticate_pairing(cfg.device_pairing_file, request.headers.get('authorization'))
        if not sid:
            raise HTTPException(401, 'device_auth_required')
        return {'ok': True, 'service': 'xiaoyun-realtime', 'session_id': sid}

    @app.websocket('/device/ws')
    async def device_socket(ws: WebSocket):
        async with pairing_lock:
            sid = authenticate_pairing(cfg.device_pairing_file, ws.headers.get('authorization'))
            if not sid:
                await ws.close(1008)
                return
            try:
                session = get_session(sid)
            except HTTPException:
                await ws.close(1008)
                return
            await ws.accept()
            # Register before releasing the pairing lock so rebinding cannot miss this socket.
            gate = DeviceGate(session, None)
            device_sockets[ws] = (session, gate)
        await serve_socket(ws, session, 'audio', device_gate=gate, accepted=True)

    async def serve_socket(ws, session, role, device_gate=None, accepted=False):
        if not accepted:
            await ws.accept()
        outbox = asyncio.Queue(maxsize=256)

        async def sink(event):
            outbox.put_nowait(event)

        async def sender():
            while True:
                await ws.send_json(await outbox.get())

        sending = asyncio.create_task(sender())
        connection = None
        gate = device_gate
        if gate:
            gate.sink = sink
        try:
            connection = await session.attach(role, gate.emit if gate else sink)
            while True:
                event = await ws.receive_json()
                if not isinstance(event, dict):
                    await sink({"type": "error", "message": "事件必须是 JSON 对象。"})
                    continue
                try:
                    if gate:
                        await gate.handle(connection, event)
                    else:
                        await session.handle(connection, event)
                except ValueError as exc:
                    await sink({"type": "error", "code": str(exc)[:100], "message": "事件未被接受，请检查当前连接和输入。"})
                except Exception:
                    await sink({"type": "error", "code": "request_failed", "message": "请求失败，请查看状态并重试连接。"})
        except (WebSocketDisconnect, asyncio.QueueFull):
            pass
        except RuntimeError:
            if ws.client_state != WebSocketState.DISCONNECTED and ws.application_state != WebSocketState.DISCONNECTED:
                raise
        except ValueError as exc:
            await ws.send_json({"type": "error", "code": str(exc), "message": "该会话通道已被其他连接占用。"})
            await ws.close(1008)
        finally:
            if gate:
                device_sockets.pop(ws, None)
                await gate.close()
            if connection:
                await session.detach(connection)
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


app = create_app()
