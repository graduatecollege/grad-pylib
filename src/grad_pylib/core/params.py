"""Reusable validated FastAPI parameter aliases."""

from typing import Annotated

from fastapi import Path, Query

_TERM_CODE_PATTERN = r"^[0-9]{6}$"
_DEPARTMENT_CODE_PATTERN = r"^[0-9]{3,4}$"
_UNIQUE_HASH_PATTERN = r"^[a-zA-Z0-9]+$"
_SNAKE_CASE_NAME_PATTERN = r"^[a-z_]+$"

TermCodePath = Annotated[
    str,
    Path(min_length=6, max_length=6, pattern=_TERM_CODE_PATTERN),
]

TermCodeQuery = Annotated[
    str,
    Query(min_length=6, max_length=6, pattern=_TERM_CODE_PATTERN),
]

DepartmentCodePath = Annotated[
    str,
    Path(min_length=3, max_length=4, pattern=_DEPARTMENT_CODE_PATTERN),
]

DepartmentCodeQuery = Annotated[
    str,
    Query(min_length=3, max_length=4, pattern=_DEPARTMENT_CODE_PATTERN),
]

UniqueHashPath = Annotated[
    str,
    Path(min_length=1, max_length=50, pattern=_UNIQUE_HASH_PATTERN),
]

SnakeCaseNamePath = Annotated[
    str,
    Path(pattern=_SNAKE_CASE_NAME_PATTERN),
]
