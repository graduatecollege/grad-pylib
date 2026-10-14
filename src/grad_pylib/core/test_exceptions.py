import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from structlog.processors import format_exc_info

from grad_pylib.core import exceptions as exceptions_module
from grad_pylib.core.exceptions import register_exception_handlers

LOST_CONNECTION_MESSAGE = "[Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x68 (104)"


class DummyDriverError(Exception):
    pass


class LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, /, **fields: object) -> None:
        self.events.append((event, fields))


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    recorder = LogRecorder()
    monkeypatch.setattr(exceptions_module, "_logger", recorder)
    return recorder


def client_raising(error: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/items")
    def create_item() -> None:
        raise error

    return TestClient(app, raise_server_exceptions=False)


def test_lost_database_connection_returns_503_with_retry_after(logs: LogRecorder) -> None:
    lost = DBAPIError(
        "INSERT INTO comments (note) VALUES (?)",
        {"note": "sensitive-note"},
        DummyDriverError("08S01", f"{LOST_CONNECTION_MESSAGE} (SQLEndTran)"),
        connection_invalidated=True,
    )

    response = client_raising(lost).post("/items")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "The database is temporarily unavailable. Please try again."}
    [(event, fields)] = logs.events
    assert event == "db.request_failed.disconnect"
    assert fields["http_method"] == "POST"
    assert fields["http_path"] == "/items"
    assert fields["sqlstate"] == "08S01"
    assert fields["during_commit"] is True

    rendered = format_exc_info(None, "warning", dict(fields))
    assert "INSERT INTO comments" not in repr(rendered)
    assert "sensitive-note" not in repr(rendered)
    traceback_text = str(rendered["exception"])
    assert LOST_CONNECTION_MESSAGE in traceback_text
    assert "in create_item" in traceback_text
    assert lost.statement == "INSERT INTO comments (note) VALUES (?)"


def test_other_database_errors_still_return_500(logs: LogRecorder) -> None:
    error = DBAPIError("select 1", {}, DummyDriverError("42000", "Syntax error (102)"))

    response = client_raising(error).post("/items")

    assert response.status_code == 500
    assert logs.events == []
