
## Pytest SQL Server integration bootstrap

`grad_pylib.testing` now includes a higher-level bootstrap object for the repeated SQL Server
integration-test wiring that application `conftest.py` files were hand-rolling around:

* shared `SharedSqlServerState`
* `pytest_configure`, `pytest_configure_node`, and `pytest_unconfigure` hooks
* session-scoped `mssql_engine`
* optional session-scoped `codebook_engine`
* per-test `db` fixture cleanup, with optional before/after hooks

### Simple app

```python
from pathlib import Path

from grad_pylib.testing import (
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
)

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

### App with cache invalidation hooks

```python
from grad_pylib.testing import SqlServerBootstrapConfig, SqlServerTestBootstrap


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

pytest_configure = BOOTSTRAP.pytest_configure
pytest_configure_node = BOOTSTRAP.pytest_configure_node
pytest_unconfigure = BOOTSTRAP.pytest_unconfigure

mssql_engine = BOOTSTRAP.mssql_engine_fixture()
codebook_engine = BOOTSTRAP.codebook_engine_fixture()
db = BOOTSTRAP.db_fixture()
```

If a service only needs the application engine and `db` fixture, leave
`include_codebook_engine=False` and omit the `codebook_engine` binding. The bootstrap still keeps
Codebook provisioning explicit through `codebook_sql_path`.
