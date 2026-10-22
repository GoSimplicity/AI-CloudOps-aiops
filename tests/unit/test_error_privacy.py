from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.middleware.error_handler import setup_error_handlers
from app.config.settings import config


def test_internal_errors_do_not_expose_credentials(monkeypatch):
    monkeypatch.setattr(config, "debug", False)
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError("postgres://user:private-secret@host/db")

    @app.get("/http")
    def http():
        raise HTTPException(500, "private-secret")

    client = TestClient(app, raise_server_exceptions=False)
    for path in ["/unexpected", "/http"]:
        response = client.get(path)
        assert response.status_code == 500
        assert "private-secret" not in response.text
        assert response.json()["data"]["error_id"]
