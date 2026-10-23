# Database pattern support: requirements and implementation plan

**Status: planned, not implemented.** This document tracks the grad-pylib work needed to
support [DB.md](DB.md). DB.md remains the application-facing standard and source for an
agent skill; implementation tasks and compatibility decisions belong here.

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
| Test database coverage | Seven test modules use SQLite engines, URLs, or driver errors | Remove all SQLite usage; use real SQL Server for database behavior and database-free tests for pure logic |

The fewer-calls, conditional-write, failure-only-diagnosis, and narrow-locking recommendations
in DB.md primarily concern application SQL and service design. They do not require a generic
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

### R6. No SQLite in tests

Remove all SQLite usage from first-party tests and supporting fixtures/configuration,
including engine creation, connection URLs, `sqlite3` exceptions, and SQLite-specific SQL
or expected results. Do not retain SQLite for speed or for tests described as generic.

Execute database-dependent tests against disposable SQL Server through the shared fixture
infrastructure. Pure mapping, query construction, URL handling, and orchestration tests may
remain database-free, using metadata, dialect compilation, and focused doubles where those
actually test the contract. Such doubles do not establish database behavior. In particular,
do not simulate pyodbc disconnect semantics using a SQLite engine with rewritten errors.

Preserve the intent and coverage of each migrated test rather than deleting difficult cases.
Use real constraints, transaction behavior, and driver errors for integration coverage.
Share container infrastructure to control startup cost, isolate databases/data by test
scope and xdist worker, and clean up committed state as well as rolled-back transactions.
Missing SQL Server infrastructure must fail required database coverage clearly, not trigger
a SQLite fallback or silently skip it.

## Implementation sequence

Each step includes its focused tests. The steps below are implementation work to perform,
not a record of completed changes.

| Step | Work | Depends on |
|---|---|---|
| 1 | Replace SQLite test usage and establish shared SQL Server coverage | None |
| 2 | Establish batch regressions and the SQLAlchemy error-integration boundary | 1 |
| 3 | Implement validated execution-time navigation and engine installation | 2 |
| 4 | Integrate the multi-result reader and direct-cursor error handling | 2, 3 |
| 5 | Add script draining and wire migration/provisioning callers | 4 |
| 6 | Verify end-to-end compatibility and publish the supported contract | 1, 3, 4, 5 |

### 1. Replace SQLite test usage

Start with the following inventory under `src/grad_pylib`, and check all test support/config
for indirect usage before declaring the migration complete:

| Test module | Migration focus |
|---|---|
| `core/test_db.py` | Runtime/session behavior on SQL Server; remove SQLite-based disconnect simulation and `sqlite3` errors |
| `core/test_multiquery.py` | Real SQL Server results for execution/mapping; retain database-free cursor doubles for isolated sequencing |
| `core/test_querying.py` | Execute fragment cases on MSSQL and use its dialect for compilation assertions |
| `core/test_auth.py` | Real SQL Server for session/transaction behavior; focused doubles only for orchestration |
| `testing/test_fixtures.py` | SQL Server app/Codebook databases, cleanup and lifecycle; replace SQLite URL-only cases without opening a database |
| `tools/test_migrate.py` | Real SQL Server migration execution, history, and transaction outcomes |
| `tools/test_generate_models.py` | MSSQL dialect/metadata for generation-only cases; real SQL Server when reflection or execution is needed |

Reuse the existing disposable container/bootstrap machinery. Make the SQL Server fixture
currently local to `core/test_db_integration.py` available through suitable shared test
fixtures rather than importing one test module from another or starting a container per
test. Keep deliberate separate connections for concurrency and multi-database cases.

Move driver-independent unit cases off database engines entirely where practical. For
disconnect/retry cases, combine deterministic driver-accurate fault injection with real
SQL Server integration coverage; preserve ambiguous-commit and cleanup assertions.
Exercise unsupported-dialect guards with focused doubles, not a replacement SQLite engine
or a newly introduced PostgreSQL server.

**Exit condition:** no SQLite engines, URLs, imports, driver exceptions, or SQL assumptions
remain in first-party tests/support; migrated coverage passes on SQL Server or is genuinely
database-free. Do not count deleting or skipping coverage as completing this step.

### 2. Establish the integration boundary

Use the shared disposable SQL Server fixtures established in step 1 to turn
the known failures into regression cases: DML before a SELECT with NOCOUNT OFF, interleaved
row counts, and errors delivered after an earlier valid rowset. Extend the cursor doubles
in `core/test_multiquery.py` for deterministic edge cases.

Confirm the error-adaptation route against the pinned SQLAlchemy 2.1.5/pyodbc 5.3.0
dependencies before implementing it. Prefer supported APIs, but require real error-event,
disconnect, and reset integration rather than a superficial wrapper. If SQLAlchemy requires
a private error-dispatch method, isolate that dependency in one typed library-local adapter
in `core/db.py` and cover it with compatibility tests. Do not duplicate SQLAlchemy's entire
error dispatcher or require application code to call a private API.

**Exit condition:** executable regressions and a demonstrated adapter design that preserves
error listeners, exception details, and invalidation for both result-associated and raw
script cursors. Dependency upgrades must rerun these compatibility cases.

### 3. Implement the option and engine wiring

Add pre-execution validation and the navigation listener in `core/db.py`. Validate only
explicit opt-in usage; keep the ordinary path inert. Make installer calls idempotent, wire
runtime and fixture engine creation, and add the public export in `__init__.py`.

Extend `core/test_db.py`, `testing/test_fixtures.py`, and `core/test_db_integration.py`.
Cover duplicate installation, unsupported use rejected before side effects, statement-level
and execute-level option resolution, zero-row DML, one final rowset, and DML-only exhaustion.
Verify `text()` and `tstring()` without swallowing empty SELECTs.

**Exit condition:** one submitted application batch can yield a normal SQLAlchemy result
without NOCOUNT scaffolding, and unsupported opt-in use fails clearly on configured engines.

### 4. Integrate reader navigation and errors

Update `core/multiquery.py::read_all_result_sets()` to use the effective option and shared
error adapter. Keep existing mapping helpers and the low-level cursor helper's contract.
Ensure result closure does not mask the primary failure or accidentally reconnect.

Extend `core/test_multiquery.py` and the SQL Server integration tests. Verify legacy
placeholders as well as opted-in omission, empty rowsets, delayed failure, `.orig`, native
error classification, `handle_error` invocation, disconnect invalidation, and reset marking.
Exercise the existing read/write retry decorators around complete reader operations; the
reader itself must never replay a cursor operation.

**Exit condition:** full traversal either returns the complete agreed data or raises a
classifiable error; retrying a whole operation cannot commit a partially successful attempt.

### 5. Drain migration and provisioning scripts

Add the shared script executor alongside cursor utilities in `core/multiquery.py`, reusing
the adapter rather than introducing another error policy. Wire the two script loops and
review administrative batch calls in fixture/model-rebuild setup for the same completion
requirement. Retain scalar setup queries as explicit result-bearing operations.

Extend `tools/test_migrate.py`, `testing/test_fixtures.py`, and real SQL Server coverage.
Add co-located model-rebuild tests for any changed setup paths; there is no dedicated
`tools/test_rebuild_models.py` yet. Test a script with a valid early result followed by a failing
statement: no migration-history success row may be committed. Also test a large discarded
rowset followed by a required write, bounded fetching, and success/failure cursor closure.
Run real script cases through both pyodbc and pymssql, including AUTOCOMMIT provisioning.

**Exit condition:** tools cannot report success merely because the first result succeeded;
SQL Server migration behavior and existing transaction/setup-lock ownership remain intact.

### 6. Verify and publish

Use the acceptance matrix below, then update docstrings, this document's status, and DB.md's
availability notice only when the contracts are implemented. Document installation for
custom engines and the intentional change from raw driver exceptions to SQLAlchemy errors.
Add the relevant compatibility note to MIGRATE.md without changing its existing retry advice.

Application adoption is a later, separate change: upgrade grad-pylib first, then opt in
specific batches and remove only scaffolding made unnecessary by complete result handling.
Do not mass-enable the option or rewrite the adjacent server projects as part of this work.

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
