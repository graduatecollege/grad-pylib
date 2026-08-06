
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

`default_name` is optional. When it is set, the instance-level convenience helpers use that
database by default, which keeps FastAPI wiring terse without relying on a hidden library-global
singleton. For named access, keep using `databases["codebook"]` or pass the name directly to
`get_engine()`, `get_session()`, `get_background_session()`, `get_runtime()`, and
`session_dependency()`.

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
