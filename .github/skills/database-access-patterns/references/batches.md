# Batch execution and result sets

SQL Server can produce non-row results between actual rowsets. With the option enabled,
the first exposed result is the first actual rowset, including an empty SELECT, rather than
a leading row-count result.

```python
result = session.execute(
    statement,
    execution_options={"mssql_skip_rowcounts": True},
)
```

This option changes result navigation only. It does not change isolation, commit, retry,
change session settings, or promise a consistent snapshot. Do not enable it globally for
ordinary ORM writes or `executemany()`: those paths have their own row-count/RETURNING behavior.
Use it for deliberate textual batches through a configured `mssql+pyodbc` engine.
`text()`, `tstring()`, textual statements with `.columns()`, and `exec_driver_sql()` are
supported. Invalid non-boolean values, non-textual ORM/Core statements, multiple parameter
sets, and unsupported dialects/drivers are rejected with `ArgumentError` before the statement
is sent. An absent or `False` option leaves ordinary execution unchanged.

## One final rowset

Use this for updates followed by one final SELECT, such as the hooding seat move:

```python
from sqlalchemy import tstring

statement = tstring(t"""
    DECLARE @changed TABLE (id int);

    UPDATE dbo.items
    SET name = {new_name}
    OUTPUT inserted.id INTO @changed
    WHERE id = {item_id};

    SELECT id FROM @changed;
""")

with session.execute(
    statement,
    execution_options={"mssql_skip_rowcounts": True},
) as result:
    changed_ids = result.scalars().all()
```

Run this inside the caller's write transaction. No `SET NOCOUNT`, cleanup-only `TRY/CATCH`,
or SQL-level `COMMIT` is needed. Ordinary result methods are appropriate only when the
row-returning statement is the final meaningful work in the batch. Do not place additional
writes or error-producing statements after it and assume `.all()` or `.close()` executes
and verifies them. Use full traversal for those batches.

A single DML statement with direct `OUTPUT` may already return the desired rows without a
batch. Prefer it when sufficient. Tables with applicable enabled triggers can require
`OUTPUT ... INTO` plus a final SELECT. OUTPUT values are not proof of a commit and may not
reflect changes made by AFTER triggers; read the persisted result if that distinction matters.

## Multiple rowsets

Use the existing multiquery reader immediately on the unconsumed result:

```python
from grad_pylib.core.multiquery import read_all_result_sets
from sqlalchemy import tstring

statement = tstring(t"""
    SELECT submission_id, term_code
    FROM dbo.student_submissions
    WHERE submission_id = {submission_id};

    SELECT student_hooder_link_id, hooder_id
    FROM dbo.student_hooder_links
    WHERE submission_id = {submission_id}
    ORDER BY display_order, student_hooder_link_id;
""")

submission_rows, hooder_rows = read_all_result_sets(
    session.execute(
        statement,
        execution_options={"mssql_skip_rowcounts": True},
    )
)
```

With the option enabled, the reader omits non-row results but retains `[]` for a real SELECT
with no rows. An empty parent result must not shift the positions of the child result sets.

Unpack a small fixed number of sets directly. For larger batches, use a service-local
`NamedTuple` with one field per expected rowset, or explicit count validation. Keep that
contract next to the SQL. Each control-flow branch must emit the agreed rowset shapes;
missing sets, unexpected sets, duplicate singleton rows, and missing required outcome rows
are programming errors, not successful empty responses.

The reader owns cursor traversal and closure. Do not call `.all()`, `.first()`, `.scalars()`,
or `.mappings()` to consume rows before handing the result to it. Consume **all** results
before committing a write or treating a batch as successful: later errors may only arrive
from `nextset()`.

The reader requires an unconsumed `CursorResult` with an active cursor: ORM entity results
raise `TypeError`, and results without an active cursor raise `ValueError`. It returns
`list[list[dict]]`; the effective execution option controls omission, not a separate reader
argument. Without the option, non-row results appear as empty lists. Ordinary single-rowset
mapping helpers, nested DTO mapping, and SQL column helpers do not require this option.

## Batches with no rowsets

Use the same execution option for a DML-only/control-flow batch. Execution completes only
after all non-row results have been traversed, surfacing any errors encountered. Close the
returned result, but do not fetch rows from it or pass its absent cursor to the
multi-result reader. If an expected DML-only batch returns columns, reject the unexpected
shape or deliberately use full traversal; do not silently close at that first rowset.

Do not use the final `result.rowcount` as the total effect of a batch. When correctness
depends on affected rows, return explicit affected keys, counts, or an outcome SELECT.
Capture `@@ROWCOUNT` immediately after the DML it describes, not after intervening statements.

## Error and connection contract

Database failures during execution or `read_all_result_sets()` consumption surface as the
matching SQLAlchemy `DBAPIError` subclass, with the original driver exception in `.orig`
and the exception chain. Catch SQLAlchemy exceptions around the reader, whether or not the
execution uses `mssql_skip_rowcounts`.
SQLAlchemy's `handle_error` listeners run, and disconnected connections are invalidated so
the existing explicit retry/error policies can handle them. Application services must not
wrap raw driver errors or reach into SQLAlchemy private methods themselves.

Programming, mapping, and validation errors retain their identity. Standalone
`cursor_rows_to_dicts()` has no SQLAlchemy connection context and remains a low-level helper
that can raise raw driver errors. The reader delegates closure to `CursorResult.close()`:
cursor-close failures are logged by SQLAlchemy, not raised over completed data or a primary error.

Do not suppress a later error because earlier rowsets were valid, or return partially collected
data as success. The reader closes its result on failure; let the transaction owner roll back.

Handling row-count results removes the need to mutate `NOCOUNT` for these patterns. It does
not solve arbitrary session-state changes. Pool rollback is not a complete reset of SET
options or committed temp tables. Prefer batch-scoped table variables; isolate exceptional
session-state changes and restore them explicitly. The installed error-only reset calls
`sp_reset_connection` when a marked live connection returns to the pool; clean returns retain
the ordinary rollback-on-return behavior. This is a fallback, not cleanup before the next
statement on the same checked-out connection. It can also clear intentional connection
initialization, which then needs restoration.
