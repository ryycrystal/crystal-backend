from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.routes import batch as batch_routes


@pytest.fixture(autouse=True)
def _transaction(monkeypatch):
    @contextmanager
    def transaction():
        yield

    monkeypatch.setattr(batch_routes, "_database_transaction", transaction)


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/value")
    def value(n: int = 0):
        return {"value": n}

    @app.get("/auth")
    def auth(request: Request):
        return {"authorization": request.headers.get("authorization")}

    app.include_router(batch_routes.router)
    return TestClient(app)


def test_batch_runs_reads_in_order():
    response = _client().post(
        "/batch",
        json={
            "requests": [
                {"id": "value", "path": "/value?n=7"},
                {"id": "next", "path": "/value?n=8"},
            ]
        },
    )

    assert response.status_code == 200
    assert [(item["id"], item["status"]) for item in response.json()["responses"]] == [
        ("value", 200),
        ("next", 200),
    ]
    assert response.json()["responses"][0]["body"] == {"value": 7}
    assert response.json()["responses"][1]["body"] == {"value": 8}


def test_batch_rolls_back_on_http_error(monkeypatch):
    state = []

    @contextmanager
    def transaction():
        before = list(state)
        try:
            yield
        except Exception:
            state[:] = before
            raise

    monkeypatch.setattr(batch_routes, "_database_transaction", transaction)
    app = FastAPI()

    @app.get("/write")
    def write():
        state.append("written")
        return {"ok": True}

    app.include_router(batch_routes.router)
    response = TestClient(app).post(
        "/batch",
        json={"requests": [{"path": "/write"}, {"path": "/missing"}]},
    )

    assert response.status_code == 409
    assert response.json()["index"] == 1
    assert state == []


def test_batch_rejects_non_read_methods():
    response = _client().post(
        "/batch",
        json={"requests": [{"method": "POST", "path": "/value", "body": {"value": 1}}]},
    )
    assert response.status_code == 400
    assert "GET and HEAD" in response.json()["detail"]


def test_batch_rejects_privileged_routes_and_headers():
    client = _client()
    for call in (
        {"path": "/results/config"},
        {"path": "/value/../results/config"},
        {"path": "/trackers/wallets/secret"},
        {"path": "/user/0x1111111111111111111111111111111111111111?include_native=false&include_native=true"},
        {"path": "/value", "headers": {"x-admin-key": "secret"}},
        {"path": "/value", "headers": {"Authorization": "Bearer secret"}},
    ):
        response = client.post("/batch", json={"requests": [call]})
        assert response.status_code == 403


def test_batch_does_not_forward_outer_credentials():
    response = _client().post(
        "/batch",
        headers={"authorization": "Bearer secret", "cookie": "session=secret"},
        json={"requests": [{"path": "/auth"}]},
    )
    assert response.status_code == 200
    assert response.json()["responses"][0]["body"] == {"authorization": None}


def test_batch_rejects_external_and_nested_calls():
    client = _client()
    for path in ("https://example.com/value", "//example.com/value", "/batch"):
        response = client.post("/batch", json={"requests": [{"path": path}]})
        assert response.status_code == 400


def test_batch_limits_request_count():
    response = _client().post("/batch", json={"requests": [{"path": "/value"}] * 21})
    assert response.status_code == 400
