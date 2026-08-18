from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


def register_exception_handlers(app: FastAPI) -> None:
    """Register the shared handler that turns raised `ApiError` values into JSON responses.

    Call once while constructing the FastAPI application so routes and dependencies can raise the
    status-specific exceptions without coupling their control flow to response construction.
    """
    app.add_exception_handler(ApiError, api_error_handler)
