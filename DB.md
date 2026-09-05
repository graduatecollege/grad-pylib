
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
