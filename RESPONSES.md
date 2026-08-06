
## API response conventions

`grad_pylib.core.schemas` now provides a small set of response helpers with one recommended shape:

* Use `data` for the primary payload.
* Add a sibling `meta` object only when the response genuinely has metadata.
* Avoid custom top-level payload keys such as `user_info`, `student`, or `results`.
* For concrete FastAPI response models, use a minimal subclass of the shared
  generic helper instead of a type alias, so the generated OpenAPI schema name
  stays short and intentional.

The shared models are:

* `ItemResponse[T]` for a single payload object
* `ListResponse[T]` for a plain list payload
* `MetaResponse[T, M]` when a `data` payload needs a sibling `meta` object
* `StatusResponse` and `build_status_response()` for `/status` endpoints

`DataResponse[T]` remains the generic base envelope that these helpers build on.

Type aliases such as `UserResponse = ItemResponse[UserDto]` work for static
typing, but FastAPI/OpenAPI derive awkward schema names from them. Prefer a
named subclass even when it has no additional fields.

### Single-item response

```python
from grad_pylib.core.schemas import CamelModel, ItemResponse


class UserDto(CamelModel):
    user_netid: str
    full_name: str


class UserResponse(ItemResponse[UserDto]):
    pass
```

### List response

Use `ListResponse[T]` when the payload is only a list:

```python
from grad_pylib.core.schemas import ListResponse


class UsersResponse(ListResponse[UserDto]):
    pass
```

If the list needs metadata, prefer a sibling `meta` object instead of inventing more top-level keys:

```python
from grad_pylib.core.schemas import CamelModel, MetaResponse


class UsersMeta(CamelModel):
    total_count: int
    next_cursor: str | None = None


class UsersResponse(MetaResponse[list[UserDto], UsersMeta]):
    pass
```

### Status endpoint response

```python
from fastapi import FastAPI

from grad_pylib.core.schemas import StatusResponse, build_status_response

app = FastAPI()


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return build_status_response(app_name="grad-service", version="1.2.3")
```

### Migration guidance

Prefer migrating existing app responses toward these conventions:

* move custom primary payload keys to `data`
* replace one-off single-item wrappers with small `ItemResponse[T]` subclasses
* replace one-off list wrappers with small `ListResponse[T]` subclasses
* replace ad hoc pagination or summary wrappers with small `MetaResponse[T, M]` subclasses
* standardize `/status` endpoints on `StatusResponse`
