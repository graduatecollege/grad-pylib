
## Database bootstrap

`grad_pylib.core.db.NamedDatabases` builds named `DatabaseRuntime` instances for FastAPI apps so
services do not need to hand-write separate engine/session/bootstrap helpers for each database.
The recommended pattern is to create one app-level `databases` singleton and export the
dependencies and helpers your routes/scripts need from that module.

```python
from grad_pylib.core.config import BaseAppSettings, get_settings
from grad_pylib.core.db import NamedDatabases


class Settings(BaseAppSettings):
    codebook_database_url: str | None = None


databases = NamedDatabases.from_settings(
    get_settings,
    {"app": "database_url", "codebook": "codebook_database_url"},
    default_name="app",
)

get_engine = databases.get_engine
get_session = databases.get_session
DbSession = databases.session_dependency()
get_background_session = databases.get_background_session

codebook_db = databases["codebook"]
get_codebook_engine = codebook_db.get_engine
get_codebook_session = codebook_db.get_session
CodebookDbSession = codebook_db.session_dependency()
get_codebook_background_session = codebook_db.get_background_session
```

`default_name` is optional. When it is set, the instance-level `get_*` helpers always use that
database. These helpers are parameterless so FastAPI cannot bind query or path parameters to
database selection, including when using `Depends(databases.get_session)` directly.

Select other databases explicitly in application setup with `databases["codebook"]`. Use its
`get_engine()`, `get_session()`, `get_background_session()`, or `runtime` attribute. Alternatively,
`databases.session_dependency("codebook")` creates a session annotation bound to that database at
setup time; it does not accept database selection from the request.

Calls such as `databases.get_session("codebook")` are no longer supported. Replace them with
`databases["codebook"].get_session()`; use the same pattern for engine and background-session
getters, and replace `databases.get_runtime("codebook")` with `databases["codebook"].runtime`.

That maps well to a typical FastAPI module layout:

```python
# app/db.py
from grad_pylib.core.config import get_settings
from grad_pylib.core.db import NamedDatabases

databases = NamedDatabases.from_settings(
    get_settings,
    {"app": "database_url", "codebook": "codebook_database_url"},
    default_name="app",
)

DbSession = databases.session_dependency()
CodebookDbSession = databases.session_dependency("codebook")
get_engine = databases.get_engine
```

```python
# app/routes/items.py
from app.db import DbSession


@router.get("/")
def read_items(session: DbSession) -> list[dict[str, object]]:
    ...
```

## Lost connections

A pooled connection can be reset mid-request by the network or the server, surfacing as ODBC
`08S01`. SQLAlchemy discards the connection and the pool recovers on its own, but the operation in
flight fails. grad-pylib handles that failure at three levels:

* **Reads:** add `@retry_on_disconnect` below the route decorator of each endpoint that only
  reads. The endpoint runs once more, after its sessions are rolled back so they reconnect.
  This works for any HTTP method, such as a search sent as POST to keep its criteria out of URLs.
  A connection lost inside a FastAPI dependency is not retried, because dependencies run before
  the endpoint.

  ```python
  @router.get("/items")
  @retry_on_disconnect
  def read_items(session: DbSession) -> ItemsResponse:
      ...
  ```

* **Writes:** `retry_on_transient_conflict` retries a lost connection along with deadlocks, lock
  timeouts, and RCSI conflicts. Use it on operations that are safe to repeat; never use
  `retry_on_disconnect` on an endpoint that writes.
* **Everything else:** `register_exception_handlers` answers a lost connection with HTTP 503 and
  `Retry-After: 1` instead of a 500. Clients should retry automatically only for safe methods.

A connection lost during commit is never retried, because the commit may already have been
applied. `is_retryable_disconnect` makes that distinction from the ODBC function pyodbc names in
its error message.

### Log events

| Event                          | Level   | Meaning                                                               |
|--------------------------------|---------|-----------------------------------------------------------------------|
| `db.disconnect`                | warning | A connection was lost; includes `connection_age_seconds`              |
| `db.disconnect.pre_ping`       | info    | Pre-ping found a dead pooled connection and replaced it transparently |
| `db.retry`                     | warning | A decorated function is retrying; `reason` names the error type       |
| `db.retry.succeeded`           | info    | A `retry_on_disconnect` retry succeeded                               |
| `db.request_failed.disconnect` | warning | The request failed with 503; includes the traceback                   |

Disconnect events carry `sqlstate`, `driver_message`, and `during_commit`; the SQL statement and
its parameters are never logged.
