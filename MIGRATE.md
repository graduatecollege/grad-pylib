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