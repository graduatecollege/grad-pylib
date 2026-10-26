# Database pattern support: requirements and implementation plan

**Status: steps 1 (SQLite removed from tests; shared SQL Server fixtures in the repo-root
`conftest.py`), 2 (batch regressions and the cursor-error adapter), 3 (option validation,
navigation, and engine installation), 4 (multi-result reader integration), and 5 (script
draining for migrations and provisioning) implemented; step 6 planned.** This document tracks the grad-pylib work needed to
support the [database access skill](.github/skills/database-access-patterns/SKILL.md).
The skill folder is the canonical application-facing guide; `DB.md` links to it.
Implementation tasks and library compatibility decisions belong here.

The plan is based on the current working tree, including its in-progress error-only
connection-reset changes. Preserve and integrate those changes rather than replacing them.
No changes to gcp-server, hooding-server, or grad-codebook are part of this implementation.

## Scope and current gaps

| Surface | Current behavior | Required change |
|---|---|---|
| Named databases, session ownership, retry decorators, upserts, error parsing | Existing primitives | Reuse; no new transaction or retry policy |
| QuerySpec, filtering, SQL fragments, DTO mapping | Existing primitives | No changes needed for these patterns |
| Textual MSSQL/pyodbc batches | A leading non-row result can cause SQLAlchemy to close the cursor before the application can advance it | Opt-in execution-time navigation |
| `read_all_result_sets()` | Non-row results and empty SELECTs both become `[]`; direct cursor failures escape as driver exceptions | Option-aware navigation and SQLAlchemy-compatible errors |
| Runtime and application fixture engines | Install the in-progress reset hook, but no batch-navigation hook | Install the same batch support consistently |
| Migration and provisioning script loops | Execute each batch without explicitly traversing all results | Drain every result before declaring success or recording a migration |

The fewer-calls, conditional-write, failure-only-diagnosis, and narrow-locking recommendations
in the skill primarily concern application SQL and service design. They do not require a generic
repository layer, automatic diagnostic queries, a universal bulk-write framework, new locking
defaults, or a global retry policy. Domain validation, authorization, audit payloads, and
conflict/replay policy remain visible in application services.

PostgreSQL is a possible future tooling backend, **not an implementation target for this
plan**. Keep SQL Server-specific behavior isolated from generic SQLAlchemy/tooling mechanics,
but do not add PostgreSQL dependencies, containers, migrations, or speculative backend
abstractions. Future PostgreSQL support must have its own real-database coverage; it must
not become another substitute for SQL Server-specific tests.

## Required contracts

### R1. Opt-in batch execution

Support `execution_options={"mssql_skip_rowcounts": True}` on deliberately textual statements
executed through a configured `mssql+pyodbc` engine. Include `text()`, `tstring()`, and textual
statements with declared columns; do not attempt to classify SQL by parsing its string.

Validate opted-in use before executing SQL. Reject unsupported dialects/drivers, ORM/Core
non-textual statements, and `executemany`; reject invalid option values. An absent or false
option leaves ordinary execution unchanged. Do not enable the option globally.

Use an `after_cursor_execute` listener to advance while `cursor.description is None`,
stopping at the first real rowset or exhaustion. Never fetch SELECT rows in this hook.
SQLAlchemy must see the correct description before constructing its result. Errors during
advancement must reach SQLAlchemy's normal error machinery.

For a DML-only batch, traversal reaches exhaustion before execution returns. A row-returning
batch exposes its first rowset even if it is empty. The option does not assert that there is
exactly one rowset, aggregate row counts, commit, retry, change isolation, or mutate `NOCOUNT`.
Additional work after a rowset still requires full traversal.

### R2. Multi-result consumption

Keep the signature and return shape of `read_all_result_sets()`. Read the effective option
from the result's execution context, not from a second caller-supplied flag.

When enabled, omit only results without a column description. Retain empty SELECTs as `[]`,
preserve rowset order, and traverse trailing non-row results to catch delayed errors.
Without the option, preserve existing non-row placeholders. Existing callers must not have
their positional result contracts silently shifted.

Require an unconsumed `CursorResult` with an active cursor, retain explicit validation
failures, and close on success or failure. Do not return partially collected data. A fully
drained DML-only execution has no active cursor and is not input to this reader.
Application-specific rowset counts and cardinality checks remain in the application.

### R3. Direct-cursor errors

Centralize handling for database exceptions from direct fetch/advance operations and the
script executor. Preserve the original driver error in `.orig`, its diagnostic codes,
exception chaining, and the appropriate SQLAlchemy `DBAPIError` subclass.

Use dialect disconnect classification and SQLAlchemy invalidation/error handling, including
existing `handle_error` listeners for disconnect logging and error-only reset marking.
Do not read connection information in a way that reconnects an invalidated connection.
Marking a connection for reset or constructing a bare `DBAPIError` is not enough.

This is an intentional exception-contract correction for the reader, including calls
without the new option: driver failures become SQLAlchemy errors. Non-driver programming,
mapping, and validation exceptions retain their existing identity. Standalone
`cursor_rows_to_dicts()` has no SQLAlchemy connection context and remains a low-level helper.

Do not add retries to the reader/hook, commit or roll back caller-owned work, double-wrap
errors already handled by SQLAlchemy, or replace the original failure with a cleanup failure.
If cleanup alone fails, surface it; if cleanup fails while handling another error, preserve
the primary exception and make the cleanup failure observable through existing logging.
Exception: the reader leaves cursor closing to `CursorResult.close()`, which logs close failures
as SQLAlchemy does for any result; after a complete traversal such a failure cannot lose data
or hide a statement error.
After a database failure, the transaction owner still decides rollback/retry.

### R4. Engine parity and compatibility

Provide an idempotent installer, provisionally `install_mssql_batch_support(engine)`, in
`core/db.py` and export it for applications that construct engines themselves. Install it in
`DatabaseRuntime.get_engine()` and `testing/fixtures.py::_create_app_engine()` so app, named,
Codebook, and E2E engines share the same behavior.

Registration must also allow configured non-MSSQL engines to reject an explicitly unsupported
option before SQL runs, while leaving all unopted-in work unchanged. Do not attach global
listeners to every SQLAlchemy engine. A custom engine that never installs support cannot be
made safe by passing an unknown execution option; document that setup requirement.

Preserve ordinary ORM flushes, generated identities, RETURNING/OUTPUT behavior, row counts,
`executemany`, single-rowset mapping, and explicit retry boundaries. Retain the existing
error-only reset as a fallback, not a reset-on-every-checkout policy or a substitute for
complete traversal. No new successful-path SQL queries or SET statements are needed.

### R5. Script execution

Provide a small shared script-batch executor for `tools/migrate.py::run_migrations()` and
`testing/fixtures.py::provision_sql_server_database_from_file()`. It executes one already
split batch, drains/discards all rowsets in bounded chunks, advances to exhaustion, and closes
its cursor. Execution, fetch, and nextset failures all propagate through the shared adapter.

Acquire the driver cursor from the supplied SQLAlchemy connection **before** execution,
not from a possibly already-closed result. Keep the same physical connection and transaction;
do not acquire a second connection or commit/roll back inside the helper. This preserves
caller-owned transactions, session-owned setup locks, and script database context.

Support both SQL Server drivers actually used here: pyodbc for application/fixture access
and pymssql for `tools/rebuild_models.py`. Do not retain a SQLite compatibility path for
tests or silently fall back to undrained execution for an unsupported SQL Server driver.
Script execution is distinct from the application rowset option and may deliberately discard
returned data. Keep driver-specific traversal behind an explicit boundary so a future
tooling backend can implement its own execution semantics without inheriting MSSQL rules.

Keep GO splitting, migration checksums, and migration-history semantics unchanged. Write the
history row only after every batch has completed traversal. Provisioning uses AUTOCOMMIT:
report delayed failures, but do not promise rollback of already-applied administrative work.
Keep explicit setup-lock return-code checks and database-existence checks; those reads decide
whether provisioning is needed and are not redundant mutation preflights.

## Implementation sequence

Each step includes its focused tests. The steps below are implementation work to perform,
not a record of completed changes.

| Step | Work | Depends on |
|---|---|---|
| 1 | **Done** | None |
| 2 | **Done** | 1 |
| 3 | **Done** | 2 |
| 4 | **Done** | 2, 3 |
| 5 | **Done** | 4 |
| 6 | Verify end-to-end compatibility and publish the supported contract | 1, 3, 4, 5 |

### 2. Integration boundary findings (done)

Regressions live in `core/test_db_integration.py` and `core/test_multiquery.py`; behavior owed
by steps 3 and 4 is pinned with strict xfails that must be flipped by those steps. The
`_Cursor` double accepts exception entries (failing `nextset`/`fetchall`) and a `close_error`.

- **Adapter:** `_sqlalchemy_cursor_errors()` in `core/db.py` is the only caller of the private
  `Connection._handle_dbapi_exception`, the method SQLAlchemy's own fetch strategies use. No
  public API dispatches `handle_error` for cursor work SQLAlchemy does not drive. Compatibility
  tests pin its signature and cover listeners, `.orig`/chaining/subclass, disconnect
  invalidation without reconnecting, and error-only reset for result and raw cursors (pyodbc,
  plus pymssql wrapping). Rerun them on any SQLAlchemy upgrade.
- SQLAlchemy 2.1.5 never assigns `ExceptionContext.cursor`; listeners must not read it.
- The adapter closes the cursor on non-disconnect errors as SQLAlchemy does, so a later
  `result.close()` logs "Error closing cursor"; step 4 should avoid that double close.
- Without traversal, errors after a rowset (`SELECT 1; SELECT 1/0` then `.all()`) and within
  DML-only batches (statement-level errors such as divide by zero) are silently lost. The
  reader currently lets raw `pyodbc.DataError` escape from `nextset()`.
- An admin `KILL` (or a severity 20+ error) ends the session and SQL Server rolls back its work.
  Mid-fetch, pyodbc reports this as SQLSTATE `HY000` "Connection may have been terminated by
  the server", which the dialect did not classify; `install_terminated_session_detection()`
  (installed by `DatabaseRuntime` and the test fixtures) now marks exactly that message as a
  disconnect, so the connection is invalidated and the usual retry rules apply. Other `HY000`
  errors, such as "Connection is busy with results for another command", are not disconnects.
  If a partly fetched rowset is skipped instead, `nextset()` reports exhaustion and a later
  `commit()` silently does nothing (the same was reported for mssql-jdbc#1505). Readers that
  fully fetch each rowset see the error; given how rare application-query kills are, no extra
  completion statements are planned.
- `install_connection_reset` uses pyodbc-only `Connection.execute`; do not install it on pymssql
  engines. `parse_mssql_error` does not parse pymssql arguments.

### 3. Option and engine wiring (done)

`install_mssql_batch_support(engine)` in `core/db.py` is exported and installed by
`DatabaseRuntime.get_engine()` and `testing/fixtures.py::_create_app_engine()`. Repeated calls
are no-ops (`event.contains` on the `before_execute` validator).

- **Validation:** `before_execute` checks `text()`/`tstring()`/`TextualSelect` executions before
  compilation and autobegin, so rejected use sends nothing and leaves no transaction. The option
  merges statement, connection, and execute levels. Non-bool values (including `None`),
  non-`mssql+pyodbc` engines, non-textual statements, and several parameter sets raise
  `sqlalchemy.exc.ArgumentError`; absent or `False` is inert, even on other engines.
- `exec_driver_sql` skips `before_execute`, so `before_cursor_execute` validates statements without
  a compiled form. It is supported, but a rejection there passes through `handle_error` and marks
  the connection for the error-only reset.
- **Navigation:** an `after_cursor_execute` listener, registered only on `mssql+pyodbc` engines,
  calls `nextset()` while `description` is `None`; failures surface as the usual SQLAlchemy
  errors (for example `DataError` with `.orig`). Connection-level `after_cursor_execute` listeners
  run before engine-level ones and so see the cursor before navigation.
- The multiquery omission test now waits only on step 4; its xfail was relabelled accordingly.

### 4. Reader navigation and errors (done)

`read_all_result_sets()` keeps its signature and return shape. It reads `mssql_skip_rowcounts`
from the result's execution context; when set, results without a description are omitted while
empty SELECTs stay `[]`. Every result is traversed, including trailing row counts.

- **Errors:** traversal runs inside `_sqlalchemy_cursor_errors`, so driver failures become the
  matching `DBAPIError` (with `.orig`, chaining, `handle_error` listeners, reset marking, and
  disconnect invalidation without reconnecting). Non-driver exceptions keep their identity.
- **Single close:** pyodbc raises on a second `close()`, which SQLAlchemy logs as "Error closing
  cursor". The reader passes no cursor to the adapter, so only `result.close()` closes it, and
  close failures are logged rather than raised. A disconnect during a read therefore also logs
  "Error closing cursor" (the connection is already closed), as plain SQLAlchemy fetches do; this
  was accepted over detaching the cursor from the result to keep the reader simple.
- A conversion error (245) aborts the batch and SQL Server rolls back the transaction itself;
  statement-level errors such as divide by zero do not, so only complete traversal stops their
  earlier writes from being committed.
- Tests: unit doubles in `core/test_multiquery.py` pin omission, trailing traversal, error
  adaptation, and close counts; SQL Server tests there and in `core/test_db_integration.py` cover
  legacy placeholders, native classification, listeners, reset, disconnects, and whole-operation
  retries with `retry_on_transient_conflict` and `retry_on_disconnect`.

### 5. Script draining (done)

`execute_script_batch(connection, batch)` in `core/multiquery.py` (exported) runs one already split
batch on a driver cursor taken from the connection before execution, fetches rowsets in chunks of
1000 and discards them, and advances to exhaustion. `run_migrations()` and
`provision_sql_server_database_from_file()` use it for every batch; GO splitting, checksums,
history semantics, the setup lock, and the existence check are unchanged.

- **Ownership:** the batch runs in the connection's current transaction, beginning one if none is
  active as `exec_driver_sql` would, so the caller commits; migration history is written in the same
  transaction only after every batch is drained. Session state carries across batches (`USE`, the
  provisioning lock). The raw execution bypasses SQLAlchemy execution events and parameters.
- **Drivers:** `_SCRIPT_DRAINERS` is the explicit boundary, with entries for `mssql+pyodbc` and
  `mssql+pymssql`; any other connection raises `ArgumentError` before anything runs.
- **Errors and closing:** execute, fetch, and nextset failures go through `_sqlalchemy_cursor_errors`
  with the batch as the statement. The adapter is given no cursor, so the executor closes it exactly
  once: a close failure is raised after a complete batch and logged as `db.script.cursor_close_failed`
  when another error is already propagating. After a disconnect the cursor is not closed, since
  invalidation closed the DB-API connection; no reconnect occurs.
- **Old behavior:** with pyodbc, an error after a valid rowset was lost and the migration recorded; on
  both drivers a write after a large rowset was silently dropped when the unread cursor was closed.
  pymssql already reported delayed errors on its next statement.
- **Setup review:** model rebuild changes nothing directly; its migrations now drain through pymssql.
  `create_database` helpers and the conftest drop run single statements without rowsets, whose errors
  are raised on execution, and the lock and existence reads stay explicit scalar queries.
- **Environment finding (fixed after step 5):** with unixODBC, pyodbc's default driver-level pooling
  kept the physical connection after `Engine.dispose()` or invalidation and handed it to the next
  connect with the same connection string, without resetting its database context, SET options, or
  session-owned app locks. `DatabaseRuntime` and the test fixtures now call `disable_pyodbc_pooling()`
  before creating engines ([SQLAlchemy recommendation](https://docs.sqlalchemy.org/en/21/dialects/mssql.html#pyodbc-pooling-connection-close-behavior)).
  pyodbc reads the setting only at its first connection. Provisioning takes and releases its setup
  lock through `master.sys`, so script `USE` statements cannot move the release to another database.
- **Provisioning error cleanup:** the administrative engine installs terminated-session detection,
  so a fatal pyodbc `HY000` error invalidates the connection and skips lock release. If lock release
  fails while another error is propagating, the primary error is preserved and cleanup is logged as
  `db.provision.lock_release_failed`; a lock-release-only failure still raises.
- Tests: doubles in `core/test_multiquery.py` pin bounded fetching, traversal, error adaptation,
  and close counts. Tests there and in `tools/test_migrate.py` and `testing/test_fixtures.py` run
  real batches on pyodbc and pymssql, covering transaction ownership, failures after a valid result
  (no history row), large discarded rowsets followed by required writes, and AUTOCOMMIT provisioning.
  `core/test_db_integration.py` covers disconnects.

### 6. Verify

Use the acceptance matrix below, then update docstrings, this document's status, and the skill's
compatibility guidance only when the contracts are implemented. Document installation for
custom engines and the intentional change from raw driver exceptions to SQLAlchemy errors.
Add the relevant compatibility note to MIGRATE.md without changing its existing retry advice.

## Acceptance and validation

| Area | Required evidence |
|---|---|
| Test backend | No SQLite usage in first-party tests/support; database behavior exercised on SQL Server; pure unit tests remain database-free |
| Navigation | Leading/intervening/trailing non-row results; zero-row DML; real empty SELECTs preserved; unchanged rowset order |
| Single/no-rowset batches | Normal scalar/mapping consumption for a final rowset; DML-only batch fully completes; unexpected rowsets are not mistaken for exhaustion |
| Error lifecycle | Execute/fetch/nextset/close failures; original error preserved; no partial successful return; later errors prevent commit |
| Connections | Non-disconnect errors reach reset marking; reset occurs on pool return; disconnects invalidate without reconnecting during cleanup |
| Retries | Opted-in whole-operation recovery for transient errors; no implicit retry; no replay after an ambiguous commit failure |
| Compatibility | Legacy reader placeholders; ordinary ORM identities/OUTPUT/rowcount/executemany; standalone mapping helpers; no NOCOUNT state mutation |
| Installation | Runtime, named/Codebook/E2E fixture engines; idempotence; unsupported opt-in use fails before SQL executes |
| Tooling | pyodbc and pymssql draining; SQL Server migration coverage; no false migration-history success; AUTOCOMMIT limitations respected |
| Efficiency | No added preflight/reset/SET executions on a clean application batch; bounded-memory script draining |

Use unit doubles for exact cursor/error sequencing and real SQL Server for driver semantics.
Check the full first-party test/support tree for remaining SQLite references, not just the
batch tests. Run the migrated test modules against SQL Server; a text search alone does not
establish that their behavior or coverage survived the migration.
Force disconnects and contention only on disposable test databases, never configured
application databases. Check NOCOUNT state before and after representative success/failure
paths and verify transaction outcomes from a separate connection.

Run the smallest relevant selectors during each step. The combined focused regression command
for the existing affected test modules is:

```bash
uv run pytest \
    src/grad_pylib/core/test_db.py \
    src/grad_pylib/core/test_auth.py \
    src/grad_pylib/core/test_querying.py \
    src/grad_pylib/core/test_multiquery.py \
    src/grad_pylib/core/test_db_integration.py \
    src/grad_pylib/testing/test_fixtures.py \
    src/grad_pylib/tools/test_migrate.py \
    src/grad_pylib/tools/test_generate_models.py
```

Include any new co-located tests in that selection. Run existing Ruff and ty
checks on changed Python modules; expand the test selection only when failures or additional
affected surfaces warrant it. No dependency additions are expected; PostgreSQL is deferred.

### Technical references

- [SQLAlchemy: multiple result sets](https://docs.sqlalchemy.org/en/21/core/connections.html#multiple-result-sets)
  documents raw-cursor traversal, not a built-in `mssql_skip_rowcounts` option.
- [SQLAlchemy: after_cursor_execute](https://docs.sqlalchemy.org/en/21/core/events.html#sqlalchemy.events.ConnectionEvents.after_cursor_execute)
  exposes the cursor before result construction; leave SELECT rows untouched.
- [Microsoft: SQLMoreResults](https://learn.microsoft.com/en-us/sql/odbc/reference/syntax/sqlmoreresults-function#comments)
  documents advancing over row counts and errors encountered during traversal.
- [SQLAlchemy: SQL Server pool resets](https://docs.sqlalchemy.org/en/21/dialects/mssql.html#temporary-table-resource-reset-for-connection-pooling)
  distinguishes rollback from wider session-state cleanup.
