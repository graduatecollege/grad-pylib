import logging
import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response

from grad_pylib.core.config import BaseAppSettings

REQUEST_ID_HEADER = "X-Request-Id"
"""Incoming header whose value is attached to logs produced while handling the request."""
REQUEST_ID_FIELD = "x_request_id"
"""Structured-log field used to record the incoming `REQUEST_ID_HEADER` value."""


def configure_logging(settings: BaseAppSettings) -> None:
    """Configure process-wide stdlib and structlog output from application settings.

    Development uses human-readable console logs and retains Uvicorn access logs; other
    environments emit JSON and suppress access logs. Call once during application startup, before
    loggers are first used, because this replaces root and Uvicorn handlers.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
    ]
    renderer = structlog.dev.ConsoleRenderer() if settings.is_development else structlog.processors.JSONRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configure_uvicorn_loggers(disable_access_logs=not settings.is_development)


def _configure_uvicorn_loggers(*, disable_access_logs: bool) -> None:
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    logging.getLogger("uvicorn.access").disabled = disable_access_logs


async def bind_request_id_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind an incoming request ID to structured logs for the lifetime of one request.

    Install as FastAPI middleware. Existing context is cleared before and after `call_next` so
    concurrent requests cannot inherit another request's correlation ID; absent headers simply
    produce logs without `REQUEST_ID_FIELD`.
    """
    structlog.contextvars.clear_contextvars()

    request_id = request.headers.get(REQUEST_ID_HEADER)
    if request_id:
        structlog.contextvars.bind_contextvars(**{REQUEST_ID_FIELD: request_id})

    try:
        response = await call_next(request)

        return response
    finally:
        structlog.contextvars.clear_contextvars()
