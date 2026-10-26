# Schema and SQL Server fixtures

## Migrations and generated models

SQL migrations are the schema source of truth. Use the project's migration runner; do not
add `metadata.create_all()` to application startup or hand-edit generated models.
The runner records filenames and checksums, and applies a file's batches in a transaction.
Applied migrations are immutable, including view definitions. Follow the consuming project's
convention for adding new view migrations and ordering their dependencies; a convention from
another service is not automatically applicable.

Regenerate models using the project's existing workflow, such as `make generate-models` or
`make rebuild-models` where those targets exist. Keep model-generation configuration responsible
for reflection quirks; keep feature behavior in services rather than generated classes.

`run_migrations()` uses `execute_script_batch(connection, batch)` to surface errors from every
batch result, and records migration history only after all batches in a file complete.
For custom migration loops, pass one already GO-split batch at a time. The helper supports
`mssql+pyodbc` and `mssql+pymssql`, drains/discards rowsets in bounded chunks, and stays on the
supplied physical connection and transaction. It begins a transaction if needed but never
commits or rolls back; SQL is sent as-is without parameters or SQLAlchemy execution events.
It is separate from the application navigation option.

## Using the SQL Server fixtures

Test selection and coverage policy belong to the consuming project. For projects using
grad-pylib's pytest fixtures, `SqlServerTestBootstrap` wires disposable SQL Server databases,
pytest/xdist lifecycle hooks, and session fixtures:

```python
from grad_pylib.testing import (
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
)
from grad_pylib.tools.migrate import run_migrations

bootstrap = SqlServerTestBootstrap(
    SqlServerBootstrapConfig(
        fixture_config=SqlServerFixtureConfig(
            migration_runner=run_migrations,
            tables_to_clean=("items",),
        ),
    )
)

pytest_configure = bootstrap.pytest_configure
pytest_configure_node = bootstrap.pytest_configure_node
pytest_unconfigure = bootstrap.pytest_unconfigure
mssql_engine = bootstrap.mssql_engine_fixture()
db = bootstrap.db_fixture()
```

Place the wiring in the project's `conftest.py`, adapting the migration runner and table names.
The fixtures use Testcontainers and require a container runtime plus ODBC Driver 18 for SQL
Server. Their application engines install the same batch and error hooks as `DatabaseRuntime`.
The `db` fixture closes its session and deletes rows from `tables_to_clean` in the supplied
order, so list dependent tables before their parents. This cleanup also removes committed
rows; rollback alone does not.

Codebook provisioning is optional: set `codebook_sql_path` when needed, and add
`include_codebook_engine=True` plus `codebook_engine = bootstrap.codebook_engine_fixture()`
when the project needs a separate Codebook engine. `before_db_test` and `after_db_test`
hooks on `SqlServerBootstrapConfig` can clear application caches.

FastAPI session overrides use the exact dependency callable exported by the app's
[database bootstrap](bootstrap.md). Concurrent work uses separate sessions/connections,
not one shared session. For additional fixture wiring, see the
[upstream fixture guide](https://github.com/graduatecollege/grad-pylib/blob/main/E2E.md).
Never run migrations or backfills against an arbitrary configured database merely to
validate a code change.
