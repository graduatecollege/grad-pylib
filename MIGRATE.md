# To v8

Use the [database access standard](DB.md) when migrating services. Opt into batch navigation
per statement, and review connection cleanup, reader exception handling, and upsert calls as
described below. The v7 transaction-owning retry and role-loader contracts are unchanged;
apply the v7 guidance below as well when upgrading from an older release.

## Engine and connection setup

Use `NamedDatabases`/`DatabaseRuntime` for application engines and the shared SQL Server
fixtures for tests. These engines install the batch and error hooks.
For engines created directly with `create_engine()`, use the
[custom-engine setup](DB.md#custom-engines); passing `mssql_skip_rowcounts` alone to an
unconfigured engine is silently ignored by SQLAlchemy.

Ensure `disable_pyodbc_pooling()` runs **before the process's first pyodbc connection**.
The runtime and fixtures call it, but earlier startup checks or direct connections must do
so themselves. Changing it after connecting is ineffective; restart with the correct startup
order. Only SQLAlchemy's pool should reuse connections.

Live connections marked by error handling are reset when returned to the pool; clean returns
are not. Do not rely on SET options, temporary objects, or connection initialization surviving
that reset. Restore intentional session state explicitly, and do not treat pool-return cleanup
as recovery for subsequent statements on the same checked-out connection.

## Opt into textual batch navigation

Add the option only to deliberate `mssql+pyodbc` batches:

```python
result = session.execute(
    statement,
    execution_options={"mssql_skip_rowcounts": True},
)
```

Supported statements are `text()`, `tstring()`, textual statements with `.columns()`, and
tooling's `exec_driver_sql()`. Do not set the option globally or use it with ordinary ORM/Core
statements, `executemany`, or another dialect/driver. Configured engines reject unsupported use
and non-boolean values with `ArgumentError` before sending the statement.

| Batch shape | Consumption |
|---|---|
| Exactly one rowset, with no meaningful work after it | Use ordinary scalar/mapping methods inside a result context manager. |
| Multiple rowsets, or any work after the first rowset | Pass the unconsumed result directly to `read_all_result_sets()`. |
| DML/control flow with no rowsets | Execution drains non-row results. Close the result; do not fetch or pass it to the reader. Reject unexpected `result.returns_rows`, or deliberately traverse them. |

The option stops at the first actual rowset, even an empty SELECT. `.all()` and `.close()`
do **not** verify later statements. Consume and validate all required outcomes before commit;
use explicit affected keys/counts, not a batch's final `rowcount`.

`read_all_result_sets()` keeps its signature and `list[list[dict]]` shape. With the option,
it omits non-row placeholders but preserves empty SELECTs as `[]`; update positional unpacking
and expected rowset counts accordingly. Without the option, legacy placeholders remain.
Do not consume any rows before calling the reader.

After opting a batch in, remove `SET NOCOUNT` scaffolding whose only purpose was result
navigation, including redundant cleanup-only TRY/CATCH blocks. Retain settings needed for
other reasons and their cleanup.
The option itself changes no session settings, isolation, commits, or retry policy.

## Adjust error handling

Driver failures inside `read_all_result_sets()` now raise the matching SQLAlchemy `DBAPIError`
subclass, **including calls without the new option**. Replace catches of raw `pyodbc.Error`
around this reader with the appropriate SQLAlchemy exception. Original diagnostics remain in
`.orig` and the exception chain; use `parse_mssql_error()` for known SQL Server conditions.
Programming/mapping/validation errors retain their identity. Standalone `cursor_rows_to_dicts()`
remains a low-level helper and does not adapt driver errors.

The reader closes its result, returns no partial data, and does not retry. Cursor-close failures
are logged by SQLAlchemy rather than replacing the primary failure. Terminated sessions are now
classified as disconnects and invalidated. Leave rollback and any whole-operation retry to the
existing owner; do not add inner commits, broad retries, or replay after an ambiguous commit.

## Account for refreshed upserts

`orm_upsert()` now refreshes already-loaded objects during its locked lookup, so stale
identity-map values cannot suppress an intended UPDATE. It **flushes pending session changes
before that refresh**, even with `autoflush=False`, then flushes the upsert. Finish and validate
pending edits before calling it: their constraints and triggers may run earlier than before.
Complete-primary-key, `insert_only`, and caller-owned commit/rollback semantics are unchanged.

## Database migrations

`run_migrations()` drains every batch, including large discarded rowsets and delayed errors.
Migration history is written only after all batches in a file succeed.
Previously hidden script failures may therefore surface during upgrades; do not suppress them
or edit already-applied migrations to bypass checksum checks. Already-recorded migrations are
not replayed or repaired automatically.

For custom migration loops, use `execute_script_batch(connection, batch)` on each already
GO-split batch. It supports `mssql+pyodbc` and `mssql+pymssql`, discards rows in bounded chunks,
and leaves commit/rollback to the caller. It sends SQL as-is, without parameters or SQLAlchemy
execution events; it is not an application query/retry wrapper.

## Test fixtures

SQL-file provisioning through `grad_pylib.testing` is **test-fixture setup only**, not a
production database setup or migration pattern. Fixture provisioning drains all batch results
and uses AUTOCOMMIT with a session-owned setup lock. A failure can leave a partially created
or seeded fixture database; recreate the affected disposable database before rerunning.
Existence alone does not establish successful fixture setup.

Fatal disconnects skip setup-lock release. Other cleanup failures are logged without masking
the primary error, or raised if cleanup alone fails.

## Service rollout

Migrate services operation by operation using [Choosing a pattern](DB.md#choosing-a-pattern).
Keep simple ORM/Core operations, preserve authorization and HTTP/domain contracts, and prefer
conditional DML/OUTPUT over redundant successful-path existence checks. Choose atomicity,
idempotency, consistency, and lock scope explicitly rather than inheriting them from a helper.

Use real SQL Server fixtures, not SQLite substitutes. Cover empty/extra rowsets, delayed
failures and rollback, stale upserts, and actual concurrent-session behavior; retain ordinary
identity/OUTPUT/executemany coverage. See [required coverage](DB.md#required-coverage-for-a-chosen-pattern).
Rerun the cursor-error compatibility tests whenever upgrading SQLAlchemy or its drivers.

# To v7

## `retry_on_transient_conflict` owns the write transaction

The decorator now begins the transaction on the decorated function's session, commits when the function returns, and rolls back when it raises. See [Retrying writes](DB.md#retrying-writes) for the full pattern.

1. **Remove `commit()` calls from decorated functions.** A statement after an inner commit now raises `InvalidRequestError`.
2. **Pass exactly one `Session`.** Decorated functions that take no session or more than one now raise `TypeError`. A function that reaches its session through a closure must take it as an argument instead.
3. **Call decorated functions with no transaction in progress.** A call raises `RuntimeError` if the session already has a transaction, including one autobegun by an earlier read or write, an outer `with session.begin():`, or an enclosing decorated function.
   - Move the reads that decide what to write into the decorated function, so a retry reruns them.
   - If an earlier read does not affect the write, end its transaction with `session.rollback()` before the call.
   - Work that must commit together with the write belongs in the same decorated function. Queries after the call are fine and run in a new transaction.
   - Application dependencies that read through the request session before the endpoint runs need to roll it back.
4. **Remove async usage.** Decorating a coroutine function now raises `TypeError`.

## Role override loaders

1. **Make loaders read-only.** The session is rolled back after `override_loader` returns or raises, so any writes are discarded. Do writes elsewhere, in their own transaction.
2. **Return plain data.** The rollback expires ORM objects loaded through the session, so build the user from values such as role names rather than keeping ORM objects on it.
3. **Optionally share the endpoint's session.** Pass `get_session=databases["app"].get_session` to the auth factory to reuse the session behind `DbSession`. `databases.get_session` is a different callable, so FastAPI opens a second session for the request.

# To v6

## Migrating a project to SQLAlchemy 2.1

1. **Fix type annotations.**
   - `Select[tuple[A, B]]` → `Select[A, B]`, and for generic code `Select[*tuple[Any, ...]]`
   - `Row[Any]`, `Result[Any]`, `CursorResult[Any]` → `Row[*tuple[Any, ...]]` and so on
   - Code that used `row._t` or `row._tuple()` only for typing can drop it.
   - `.scalar()` is now typed as possibly `None`. Use `.scalar_one()` where a value is guaranteed.
2. **Check runtime changes.** Each of these only matters if the app does the thing described:
   - **Autoflush:** sessions not created with `autoflush=False` now flush before every query, including `text()` queries. Sessions from grad-pylib's `DatabaseRuntime` are unaffected.
   - **ODBC connection strings:** if any contained `%2B` as a workaround for `+`, change it back to a literal `+`. Passwords containing `+` now connect correctly.
   - **`params()` on expressions:** calling `.params()` on a column or condition, rather than on a whole statement, now gives a deprecation warning. Call it on the statement instead.
   - **`like`/`ilike` on non-string columns:** these now give a deprecation warning.