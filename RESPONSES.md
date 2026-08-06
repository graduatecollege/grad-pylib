
## API response conventions

`grad_pylib.core.schemas` now provides a small set of response helpers with one recommended shape:

* Use `data` for the primary payload.
* Add a sibling `meta` object only when the response genuinely has metadata.
* Avoid custom top-level payload keys such as `user_info`, `student`, or `results`.

The shared models are:

* `ItemResponse[T]` for a single payload object
* `ListResponse[T]` for a plain list payload
* `MetaResponse[T, M]` when a `data` payload needs a sibling `meta` object
* `StatusResponse` and `build_status_response()` for `/status` endpoints

`DataResponse[T]` remains the generic base envelope that these helpers build on.

### Single-item response

```python
from grad_pylib.core.schemas import CamelModel, ItemResponse


class UserDto(CamelModel):
    user_netid: str
    full_name: str


UserResponse = ItemResponse[UserDto]
```

### List response

Use `ListResponse[T]` when the payload is only a list:

```python
from grad_pylib.core.schemas import ListResponse

UsersResponse = ListResponse[UserDto]
```

If the list needs metadata, prefer a sibling `meta` object instead of inventing more top-level keys:

```python
from grad_pylib.core.schemas import CamelModel, MetaResponse


class UsersMeta(CamelModel):
    total_count: int
    next_cursor: str | None = None


UsersResponse = MetaResponse[list[UserDto], UsersMeta]
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
* replace one-off single-item wrappers with `ItemResponse[T]`
* replace one-off list wrappers with `ListResponse[T]`
* replace ad hoc pagination or summary wrappers with `MetaResponse[T, M]`
* standardize `/status` endpoints on `StatusResponse`
