import copy
from http import HTTPStatus

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from grad_pylib.core.db import describe_disconnect, is_disconnect_error

DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS = 1
"""`Retry-After` value sent with the 503 response for a lost database connection."""

_logger = structlog.get_logger(__name__)


class ApiError(Exception):
    """Base exception for expected client-facing API failures.

    Raise a status-specific subclass from route or dependency code after registering
    `register_exception_handlers`; the shared handler exposes only this exception's message.
    """
    def __init__(self, status_code: int, message: str) -> None:
        """Create an API failure with the response status and safe client message."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class BadRequestError(ApiError):
    """Expected request validation failure returned to the client as HTTP 400."""
    def __init__(self, message: str) -> None:
        """Create an HTTP 400 response error with a safe explanation for the client."""
        super().__init__(HTTPStatus.BAD_REQUEST, message)


class ForbiddenError(ApiError):
    """Authorization failure returned to the client as HTTP 403."""
    def __init__(self, message: str) -> None:
        """Create an HTTP 403 response error with a safe explanation for the client."""
        super().__init__(HTTPStatus.FORBIDDEN, message)


class NotFoundError(ApiError):
    """Missing resource failure returned to the client as HTTP 404."""
    def __init__(self, message: str) -> None:
        """Create an HTTP 404 response error with a safe explanation for the client."""
        super().__init__(HTTPStatus.NOT_FOUND, message)


class ConflictError(ApiError):
    """Request-state conflict returned to the client as HTTP 409."""
    def __init__(self, message: str) -> None:
        """Create an HTTP 409 response error with a safe explanation for the client."""
        super().__init__(HTTPStatus.CONFLICT, message)


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Translate registered `ApiError` instances into the library's `detail` response shape.

    The defensive fallback does not expose an unexpected exception's message should this handler
    be registered more broadly than `register_exception_handlers` does.
    """
    if isinstance(exc, ApiError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
    return JSONResponse(status_code=HTTPStatus.INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


async def database_disconnect_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a lost database connection with HTTP 503 so clients can tell it apart from a bug.

    The pool replaces the lost connection, so a later attempt is expected to succeed; the response
    carries `Retry-After` and is logged as `db.request_failed.disconnect`. Clients should
    automatically retry only safe methods, since a write may have committed before the connection
    was lost. Other database errors are re-raised unchanged for the default 500 handling.
    """
    if not is_disconnect_error(exc):
        raise exc

    _logger.warning(
        "db.request_failed.disconnect",
        http_method=request.method,
        http_path=request.url.path,
        exc_info=(type(exc), _without_statement(exc), exc.__traceback__),
        **describe_disconnect(exc),
    )
    return JSONResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        content={"detail": "The database is temporarily unavailable. Please try again."},
        headers={"Retry-After": str(DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS)},
    )


def _without_statement(exc: DBAPIError) -> DBAPIError:
    # The error's message would otherwise include the SQL statement and its parameter values. The
    # copy also drops the exception chain; the driver's message is already part of its own message.
    sanitized = copy.copy(exc)
    sanitized.statement = None
    sanitized.params = None
    return sanitized


def register_exception_handlers(app: FastAPI) -> None:
    """Register the shared handlers that turn raised `ApiError` values and lost database connections into JSON responses.

    Call once while constructing the FastAPI application so routes and dependencies can raise the
    status-specific exceptions without coupling their control flow to response construction.
    """
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(DBAPIError, database_disconnect_handler)
