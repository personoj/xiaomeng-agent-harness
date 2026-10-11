from fastapi.testclient import TestClient

from xiaoyun.app import create_app
from xiaoyun.config import Settings


def test_http_and_page_only_websocket(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["service"] == "xiaoyun-realtime"
        sid = client.post("/api/sessions", json={}).json()["session_id"]
        with client.websocket_connect(f"/ws/{sid}?role=page") as ws:
            assert ws.receive_json()["type"] == "attached"
            assert ws.receive_json()["type"] == "integration.updated"
            ws.send_json({"type": "context.update", "context": {"page": "a"}})
            assert ws.receive_json()["revision"] == 1
            assert ws.receive_json()["type"] == "integration.updated"
            ws.send_json({"type": "input.text", "text": "no"})
            assert ws.receive_json()["code"] == "channel_role_mismatch"
        assert client.get(f"/api/sessions/{sid}").json()["context"] == {}


def test_foreign_origins_and_invalid_ids_rejected(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post("/api/sessions", json={}, headers={"origin": "https://example.com"}).status_code == 403
        assert client.post("/api/sessions", json={"session_id": "../outside"}).status_code == 400


def test_local_frontend_different_port_can_load_sdk_and_create_session(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        origin = "http://localhost:5173"
        sdk = client.get("/static/frontend.js", headers={"origin": origin})
        assert sdk.status_code == 200
        assert sdk.headers["access-control-allow-origin"] == origin
        preflight = client.options("/api/sessions", headers={"origin": origin, "access-control-request-method": "POST", "access-control-request-headers": "Content-Type"})
        assert preflight.status_code == 200
        assert client.post("/api/sessions", json={}, headers={"origin": origin}).status_code == 200
