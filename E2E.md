# SQL Server test fixtures

`grad_pylib.testing.SqlServerTestBootstrap` provides shared integration-test wiring for
application `conftest.py` files:

* shared `SharedSqlServerState`
* `pytest_configure`, `pytest_configure_node`, and `pytest_unconfigure` hooks
* session-scoped `mssql_engine`
* optional session-scoped `codebook_engine`
* per-test `db` fixture cleanup, with optional before/after hooks

The fixture factories return plain pytest fixture functions that retain the bootstrap
instance, so the same module-level bindings support both function-based and class-based tests.

Install the library's `test` extra in consuming projects. The fixtures require a container
runtime and ODBC Driver 18 for SQL Server. For session cleanup and migration guidance, see
[Schema and SQL Server fixtures](.github/skills/database-access-patterns/references/schema-testing.md).

SQL Server containers use a `2g` Docker memory limit. Before provisioning databases, startup
sets `max server memory (MB)` to `1024` through `sp_configure` and `RECONFIGURE`, leaving
headroom for SQL Server allocations outside that setting. This applies to shared xdist,
sequential, E2E, and model-rebuilding containers; `MSSQL_MEMORY_LIMIT_MB` is not set.

## App with a Codebook database

```python
from pathlib import Path

from grad_pylib.testing import (
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
)
from grad_pylib.tools.migrate import run_migrations

FIXTURE_CONFIG = SqlServerFixtureConfig(
    migration_runner=run_migrations,
    tables_to_clean=("example_table",),
)
CODEBOOK_SQL = Path(__file__).parent / "data" / "codebook_minimal.sql"

BOOTSTRAP = SqlServerTestBootstrap(
    SqlServerBootstrapConfig(
        fixture_config=FIXTURE_CONFIG,
        codebook_sql_path=CODEBOOK_SQL,
        include_codebook_engine=True,
    )
)

pytest_configure = BOOTSTRAP.pytest_configure
pytest_configure_node = BOOTSTRAP.pytest_configure_node
pytest_unconfigure = BOOTSTRAP.pytest_unconfigure

mssql_engine = BOOTSTRAP.mssql_engine_fixture()
codebook_engine = BOOTSTRAP.codebook_engine_fixture()
db = BOOTSTRAP.db_fixture()
```

If a service only needs the application engine and `db` fixture, omit `codebook_sql_path`,
leave `include_codebook_engine=False`, and omit the `codebook_engine` binding. A service can
also set `codebook_sql_path` to provision Codebook without exposing a separate engine fixture.

## Cache invalidation hooks

Add hooks to the bootstrap configuration when tests need to clear application caches.
Keep the hook and fixture bindings from the example above:

```python
def clear_test_caches() -> None:
    cache.clear()


BOOTSTRAP = SqlServerTestBootstrap(
    SqlServerBootstrapConfig(
        fixture_config=FIXTURE_CONFIG,
        codebook_sql_path=CODEBOOK_SQL,
        include_codebook_engine=True,
        before_db_test=clear_test_caches,
        after_db_test=clear_test_caches,
    )
)
```
