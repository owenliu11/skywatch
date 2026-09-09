"""Phase 3 placeholder and browser-origin acceptance."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.main import app


@pytest.mark.parametrize("query", ["", "?since=&type=", "?since=2026-09-08T00:00:00Z&type=teleport"])
def test_anomalies_stub(query):
    response = TestClient(app).get("/anomalies" + query)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:5173.evil.example"])
def test_disallowed_origin(origin):
    with (
        pytest.raises(WebSocketDisconnect) as error,
        TestClient(app).websocket_connect("/ws/live", headers={"origin": origin}),
    ):
        pytest.fail("untrusted origin was accepted")
    assert error.value.code == 1008


@pytest.mark.parametrize("origin", [None, "https://skywatch.example"])
def test_allowed_origin(monkeypatch, origin):
    monkeypatch.setattr(settings, "ws_allowed_origins", "https://skywatch.example")
    headers = {} if origin is None else {"origin": origin}
    with TestClient(app).websocket_connect("/ws/live", headers=headers) as socket:
        socket.send_json({"t": "invalid"})
        assert socket.receive_json()["t"] == "error"
