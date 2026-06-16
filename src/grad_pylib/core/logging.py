import logging
import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response

from grad_pylib.core.config import BaseAppSettings

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_FIELD = "x_request_id"


def configure_logging(settings: BaseAppSettings) -> None:
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
    structlog.contextvars.clear_contextvars()

    request_id = request.headers.get(REQUEST_ID_HEADER)
    if request_id:
        structlog.contextvars.bind_contextvars(**{REQUEST_ID_FIELD: request_id})

    start_time = time.perf_counter()
    structlog.get_logger("http.access").info(
        "request_started",
        method=request.method,
        path=request.url.path,
    )
    try:
        response = await call_next(request)
        duration = time.perf_counter() - start_time
        structlog.get_logger("http.access").info(
            "request_finished",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response
    finally:
        structlog.contextvars.clear_contextvars()
