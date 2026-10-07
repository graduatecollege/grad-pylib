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