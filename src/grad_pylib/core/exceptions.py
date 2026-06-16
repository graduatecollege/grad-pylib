from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class BadRequestError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(HTTPStatus.BAD_REQUEST, message)


class ForbiddenError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(HTTPStatus.FORBIDDEN, message)


class NotFoundError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(HTTPStatus.NOT_FOUND, message)


class ConflictError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(HTTPStatus.CONFLICT, message)


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ApiError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
    return JSONResponse(status_code=HTTPStatus.INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
