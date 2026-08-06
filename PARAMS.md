# Parameter types

`grad_pylib` provides shared validated types for common request parameters and request body fields.

## FastAPI parameter aliases

Use these in route signatures when the value comes from the path or query string:

* `TermCodePath`
* `TermCodeQuery`
* `DepartmentCodePath`
* `DepartmentCodeQuery`
* `UniqueHashPath`
* `SnakeCaseNamePath`

These aliases replace repeated inline validation such as:

```python
from typing import Annotated

from fastapi import Path, Query

term_code: Annotated[str, Path(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")]
department_code: Annotated[str, Query(min_length=3, max_length=4, pattern=r"^[0-9]{3,4}$")]
unique_hash: Annotated[str, Path(min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9]+$")]
table_name: Annotated[str, Path(pattern=r"^[a-z_]+$")]
```

Applications can instead use clearer shared aliases:

```python
from fastapi import APIRouter

from grad_pylib import DepartmentCodeQuery, SnakeCaseNamePath, TermCodePath, UniqueHashPath

router = APIRouter()


@router.get("/terms/{term_code}/records/{unique_hash}")
def read_record(
    term_code: TermCodePath,
    unique_hash: UniqueHashPath,
    department_code: DepartmentCodeQuery | None = None,
) -> dict[str, str | None]:
    return {
        "term_code": term_code,
        "unique_hash": unique_hash,
        "department_code": department_code,
    }


@router.get("/tables/{table_name}")
def read_table(table_name: SnakeCaseNamePath) -> dict[str, str]:
    return {"table_name": table_name}
```

## Reusable Pydantic field types

Use these in request body models and other Pydantic models:

* `TermCode`
* `DepartmentCode`
* `UniqueHash`
* `SnakeCaseName`

These types carry the same validation rules as the shared parameter aliases, but use Pydantic
constraints instead of FastAPI `Path(...)` or `Query(...)` metadata.

```python
from pydantic import BaseModel

from grad_pylib import DepartmentCode, SnakeCaseName, TermCode, UniqueHash


class RecordLookupRequest(BaseModel):
    term_code: TermCode
    department_code: DepartmentCode
    unique_hash: UniqueHash
    table_name: SnakeCaseName
```

## Validation rules

| Type | Rule |
| --- | --- |
| `TermCodePath`, `TermCodeQuery`, `TermCode` | exactly 6 numeric characters |
| `DepartmentCodePath`, `DepartmentCodeQuery`, `DepartmentCode` | 3 to 4 numeric characters |
| `UniqueHashPath`, `UniqueHash` | 1 to 50 alphanumeric characters |
| `SnakeCaseNamePath`, `SnakeCaseName` | lowercase letters and underscores only |

Keep other identifiers local to the consuming application when they are only used by one service or
carry service-specific business rules.
