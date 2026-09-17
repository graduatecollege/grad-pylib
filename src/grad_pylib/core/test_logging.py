import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from grad_pylib.core.config import BaseAppSettings
from grad_pylib.core.logging import (
    REQUEST_ID_FIELD,
    REQUEST_ID_HEADER,
    bind_request_id_context,
    configure_logging,
)


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    previous_structlog_config = structlog.get_config()
    logger_names = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")
    previous_logger_states = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate, logging.getLogger(name).disabled)
        for name in logger_names
    }
    output = io.StringIO()

    configure_logging(BaseAppSettings(environment="production"))
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(output)
    try:
        yield output
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.configure(**previous_structlog_config)
        root.handlers.clear()
        root.handlers.extend(previous_handlers)
        root.setLevel(previous_level)
        for name, (handlers, propagate, disabled) in previous_logger_states.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.disabled = disabled


class _TopLevelExceptionLogger:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")


@pytest.mark.parametrize("request_id", [None, "provided-request-id"])
def test_unhandled_exception_log_contains_request_id(
        log_output: io.StringIO,
        request_id: str | None,
) -> None:
    app = FastAPI()
    app.middleware("http")(bind_request_id_context)

    @app.get("/fail")
    def fail() -> None:
        raise RuntimeError("request failed")

    headers = {} if request_id is None else {REQUEST_ID_HEADER: request_id}
    with TestClient(_TopLevelExceptionLogger(app)) as client:
        response = client.get("/fail", headers=headers)

    assert response.status_code == 500
    event = next(
        event
        for line in log_output.getvalue().splitlines()
        if (event := json.loads(line))["level"] == "error"
    )
    logged_request_id = event[REQUEST_ID_FIELD]
    if request_id is None:
        assert str(UUID(logged_request_id)) == logged_request_id
    else:
        assert logged_request_id == request_id


def test_stdlib_exception_log_renders_traceback(log_output: io.StringIO) -> None:
    try:
        raise RuntimeError("SQL execution failed")
    except RuntimeError:
        logging.getLogger("test.sql").exception("Database query failed")

    event = json.loads(log_output.getvalue())

    assert "Traceback (most recent call last)" in event["exception"]
    assert __file__ in event["exception"]
    assert "raise RuntimeError(\"SQL execution failed\")" in event["exception"]
