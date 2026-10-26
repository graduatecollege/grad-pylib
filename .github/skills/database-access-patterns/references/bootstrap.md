# Database bootstrap, sessions, engines, and multiple databases

Create one app-level registry. Resolve database names during application setup, not from
request input. Export stable dependency callables so tests can override the same objects.

```python
from grad_pylib.core.db import NamedDatabases

from core.config import get_settings

databases = NamedDatabases.from_settings(
    get_settings,
    {"app": "database_url", "codebook": "codebook_database_url"},
    default_name="app",
)

app_db = databases["app"]
get_engine = app_db.get_engine
get_session = app_db.get_session
get_background_session = app_db.get_background_session
DbSession = app_db.session_dependency()

codebook_db = databases["codebook"]
get_codebook_engine = codebook_db.get_engine
get_codebook_session = codebook_db.get_session
CodebookDbSession = codebook_db.session_dependency()
```

A single-database service uses only `{"app": "database_url"}`. The settings provider returns
an app-specific settings object; ODBC strings stay in configuration, never in source or logs.
See `BaseAppSettings`, `build_mssql_url()`, and `resolve_database_url()`.

Use this same `get_session` callable for auth and test overrides. If an app wraps it in a
local function, define the annotation as `Annotated[Session, Depends(get_session)]` using that
wrapper everywhere. The registry's default `databases.get_session` wrapper and the named
database's `app_db.get_session` are different dependencies; do not mix them accidentally.

Engines and session factories are shared; `Session` objects are not. Use one session per
request/task, never a module-global session, and never use a session concurrently.
Services receive a `Session`; they do not open a hidden second one to bypass their caller's
transaction. FastAPI dependency aliases belong at the route boundary.

`DatabaseRuntime` sessions use `autoflush=False` and `expire_on_commit=False`. Flush explicitly
before a query that depends on pending ORM changes, before reading generated identifiers, or
when a constraint must be checked at a particular point. `flush()` is not a commit.
Non-expiring objects are convenient, but are not a promise that their values remain current.

Closing a request/background session releases resources and rolls back outstanding work.
It does **not** commit successful requests. Do not move commits into dependency teardown,
where failures can occur after a response has been prepared.

Let `DatabaseRuntime` own pool configuration. Its pre-ping checks connections on checkout,
and its default 300-second recycle replaces sufficiently old connections on checkout.
Neither prevents a mid-operation disconnect or retries an operation. Size pools against
the number of application processes and the server's connection budget; do not create a
new engine per request or dispose the shared engine after each operation.

Creating a `DatabaseRuntime` engine also calls `disable_pyodbc_pooling()`, as SQLAlchemy
recommends: pyodbc's driver-level pool otherwise keeps sessions alive after the engine discards
them and reuses them without a reset. The setting is process-wide and is read only at the first
pyodbc connection, so code that connects through pyodbc before any runtime engine exists, such
as a startup check, must call `disable_pyodbc_pooling()` first.

## Custom engines

Prefer `DatabaseRuntime`, including through `NamedDatabases`. Its engines and the shared
application/Codebook/E2E fixture engines install batch navigation, terminated-session
detection, and error-only reset. Do not install those hooks again on managed engines.

If an application must construct its own engine, install the same hooks once at setup,
before using it. Here `database_url` is a configured **`mssql+pyodbc` SQLAlchemy URL**:

```python
from sqlalchemy import create_engine
from grad_pylib.core.db import (
    DEFAULT_POOL_RECYCLE_SECONDS,
    disable_pyodbc_pooling,
    install_connection_reset,
    install_mssql_batch_support,
    install_terminated_session_detection,
)

disable_pyodbc_pooling()
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_recycle=DEFAULT_POOL_RECYCLE_SECONDS,
)
install_terminated_session_detection(engine)
install_connection_reset(engine)
install_mssql_batch_support(engine)
```

The pooling call must precede the first pyodbc connection anywhere in the process, not merely
this engine's first checkout. `install_mssql_batch_support()` is idempotent, but it installs
only option validation/navigation, not the other connection hooks. Without it, SQLAlchemy
silently ignores the unknown execution option. It can also be installed on other engines to
reject unsupported opt-in use while leaving ordinary execution unchanged.

The full recipe above is for `mssql+pyodbc`; the reset hook uses pyodbc-specific connection
methods. Tools using `pymssql` should use the script executor described under
[Migrations and generated models](schema-testing.md#migrations-and-generated-models), not this application setup.

## Multiple databases

Use an app session for app data and a Codebook session for independent reference lookups.
Treat Codebook/warehouse data as read-only in request code. Where deployed schemas already
expose same-server cross-database views, querying those views through the app session is also
valid; do not dynamically build database names from user input.

Two sessions are two transactions. Committing one and then the other is not atomic, and a
read across both is not one consistent snapshot. Cross-database writes require an explicit
design, not nested session context managers. Keep them out of an ordinary single-session
retryable write.
