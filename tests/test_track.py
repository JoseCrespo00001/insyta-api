from fastapi.testclient import TestClient

from app.main import app


def test_track_returns_503_with_eta():
    with TestClient(app) as client:
        r = client.post("/api/v1/track", json={"event": "x"})
        assert r.status_code == 503
        body = r.json()
        assert body["eta"] == "Sprint 2"
        assert "beta" in body["error"].lower()


def test_message_returns_503_with_eta():
    with TestClient(app) as client:
        r = client.post("/api/v1/message", json={"text": "hi"})
        assert r.status_code == 503
        body = r.json()
        assert body["eta"] == "Sprint 2"
