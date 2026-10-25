# Database access patterns

This is the database-access standard for Graduate College Python services. It is written as
an implementation guide and a source for an AI agent skill, not a catalogue of every existing
implementation. Priorities, in order: **correctness, ease of understanding, performance,
maintainability**.

The patterns cover synchronous SQLAlchemy access to SQL Server through `mssql+pyodbc`, using
Python 3.14/3.15. FastAPI endpoints and dependencies are synchronous. SQLAlchemy ORM/Core is
the first choice for routine work; SQLAlchemy `tstring(t"...")` is the preferred way to write
new application SQL batches and SQL fragments.

## Status and scope

This guide describes the **target standard**. Most primitives already exist, but the
batch/result examples using `mssql_skip_rowcounts` depend on **unimplemented library support**.
It is a proposed grad-pylib extension, not a built-in SQLAlchemy option; adding an unknown
execution option alone does not change SQLAlchemy's behavior.

Library requirements, compatibility details, and the implementation plan are maintained
separately in [DB-IMPLEMENTATION.md](DB-IMPLEMENTATION.md). This document describes how
application code should use the patterns, not how to implement the library support.

This guide standardizes mechanics without making every endpoint behave identically. Existing
HTTP contracts, authorization rules, and domain-specific normalization must not change as a
side effect of adopting a database helper. Exceptions to a recommendation should state the
reason and have coverage for the behavior they preserve.

## Choosing a pattern

Before changing database code:

1. Identify the database, schema objects, caller, and public response contract.
2. Decide whether the operation only reads, writes once, or may safely replay a complete write
   transaction. A POST can be read-only; a seemingly read-oriented service can still write.
3. Choose the result shape and consistency requirement: scalar, optional row, list, joined
   object, multiple rowsets, or a write outcome.
4. Identify the transaction owner, concurrency guard, and any audit, replay, cache, or external
   side effects. Count successful-path database calls and identify the rows locked and the
   round trips between acquiring those locks and committing.
5. Select the simplest pattern below; make these choices visible in the calling code.

| Need | Preferred pattern |
|---|---|
| One entity or a short list | ORM/Core `select`, with explicit result cardinality |
| A read-only view or projected DTO | Core columns plus `.mappings()` and DTO validation |
| Nested objects from one join | Core tables/aliases plus `map_row_to_pydantic()` |
| Search, filtering, sorting, paging | `QuerySpec`, fixed authorization predicates, deterministic ordering |
| Several related collections or page plus count | One batch plus `read_all_result_sets()` |
| DML followed by one final rowset | Batch option plus ordinary SQLAlchemy result consumption |
| DML-only batch | Batch option; advance through all non-row results before reporting completion |
| One database write operation | An explicitly owned transaction, with or without retries |
| Edit only if a value is unchanged | Conditional DML plus verified affected keys/outcome |
| Insert or replace fields by complete primary key | `orm_upsert()` inside the caller's transaction |
| Optional state, overrides, or JSON edits | Explicit absence/clear semantics and protected field-level updates |
| Delete, merge, or reassign relationships | One transaction with explicit ownership, conflict, and history rules |
| Several records changed together | Atomic, set-oriented bulk operation; partial success only by explicit contract |
| External lookup followed by a write | Prepare outside the write transaction, then revalidate and apply |
| Cached read or polling marker | Plain-data cache, authorized access, post-commit invalidation |
| Backfill, export, or maintenance | Explicit session lifetime and transaction boundaries; no request session reuse |

Ask the caller when the requirement does not settle: exact snapshot consistency, conflict
behavior, idempotency scope, partial versus atomic success, bounded versus streaming results,
or whether an operation is replay-safe. Do not invent retries or silently discard conflicts.

### Successful-path calls and failure diagnostics

Favor fewer database calls over more specific error messages. Do not query whether a student
exists before modifying it when the UPDATE/DELETE predicate and affected-row result already
answer whether the operation succeeded. Likewise, use joins, conditional DML, OUTPUT, and
database constraints to enforce conditions instead of duplicating them in preflight queries.
Required authorization and invariants must still be enforced, not omitted to save a call.

A useful general error such as "The record changed or is no longer available" is preferable
to an extra query on every successful request merely to distinguish missing from stale.
If a more specific diagnosis is useful and simple, query only after the statement fails or
affects no expected rows. Follow the transaction/error rules in
[Failure-only diagnostics](#failure-only-diagnostics).

This does not prohibit reads whose data is needed to compute a change or produce a response.
Prefer returning needed values from the mutation or its batch when that keeps the code clear.
Do not replace a readable operation with opaque SQL just to remove a round trip. Correctness
and ease of understanding still take precedence.

## Database bootstrap

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
request/task, never a module-global session, and never share a session between threads.
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

### Multiple databases

Use an app session for app data and a Codebook session for independent reference lookups.
Treat Codebook/warehouse data as read-only in request code. Where deployed schemas already
expose same-server cross-database views, querying those views through the app session is also
valid; do not dynamically build database names from user input.

Two sessions are two transactions. Committing one and then the other is not atomic, and a
read across both is not one consistent snapshot. Cross-database writes require an explicit
design, not nested session context managers. Keep them out of an ordinary single-session
retryable write.

## Reads and result mapping

Examples use illustrative application models such as `Item`, DTOs such as `ItemDto`, and
service-local helpers. These are not additional grad-pylib APIs.

### Routine reads

Use the result method that expresses the expected cardinality:

| Expected result | Form |
|---|---|
| ORM entity by primary key, possibly absent | `session.get(Item, item_id)` |
| Exactly one scalar/entity | `session.execute(stmt).scalar_one()` |
| Zero or one scalar/entity | `session.execute(stmt).scalar_one_or_none()` |
| Exactly one projected row | `session.execute(stmt).mappings().one()` |
| Zero or one projected row | `session.execute(stmt).mappings().one_or_none()` |
| List of entities or first-column values | `session.scalars(stmt).all()` |
| List of projected rows | `session.execute(stmt).mappings().all()` |

Use `.first()` only when selecting one candidate is intentional, with deterministic ordering
and a SQL limit where appropriate. `.first()` does not add `TOP 1` for you. Do not use it to
hide unexpected duplicate rows. A scalar nullable column cannot distinguish "row absent" from
"column is NULL"; retrieve a row or a non-null identifier when that distinction matters.

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

def find_item(session: Session, item_id: int) -> ItemDto | None:
    row = session.execute(
        select(Item.id, Item.name).where(Item.id == item_id)
    ).mappings().one_or_none()
    return None if row is None else ItemDto.model_validate(row)
```

Choose an explicit not-found boundary: an optional lookup returns `None`; a required service
operation raises `NotFoundError`. Do not return empty success-shaped objects for failed
required lookups. This is a result contract, not a requirement to perform an existence lookup
before a later statement that can establish the same condition itself.
Build DTOs before releasing the session; avoid lazy database access during
response serialization. For ORM relationships, choose eager loading intentionally rather
than causing a query per row. Read projections usually need no ORM identity tracking.

Use schema-derived columns, not handwritten copies of mapped tables. Views without a genuine
unique key are Core `Table` objects, not ORM entities with an invented primary key.
`select_exclude()` can reduce loaded columns, but deferred ORM columns are **not** a
redaction or authorization boundary.

### Joined and nested DTOs

Use Core table/alias projections when one row contains several nested objects:

```python
from grad_pylib.core.multiquery import map_row_to_pydantic
from sqlalchemy import select

row = session.execute(
    select(programs, departments)
    .outerjoin(departments, programs.c.department_id == departments.c.id)
    .where(programs.c.code == code)
).one_or_none()

details = None if row is None else map_row_to_pydantic(
    row,
    ProgramDetails,
    nest_mappings={"program": programs, "department": departments},
)
```

The mapping must use the same tables/aliases selected by the query. Select Core columns,
not ORM entities, for this helper. Include a non-null key for each optional joined object:
an all-NULL section becomes `None`. DTO field names or validation aliases must match the
physical column names used by the helper.

If SQL text is necessary, provide `.columns(...)` metadata for column-aware mapping, or use
`qualified_columns()` with `split_row_sections()` for explicitly ordered positional sections.
Keep the projection and section definitions together. Positional mapping is the exception,
not the default for routine joins.

Multi-result dictionaries require unique column names within each rowset. Alias duplicate
names instead of silently overwriting them. The multi-result reader returns driver values,
not per-column SQLAlchemy result processing for every subsequent rowset; DTO validation and
explicit conversions must account for that.

### Filtering, sorting, paging, and exports

Declare request filters with `FilterField`/`create_filter_model` and allowed SQL columns with
`QuerySpec`. Use `filter_values()` to exclude pagination, search, and other non-column fields.
Apply request filters in addition to fixed term, soft-delete, and authorization predicates.
Do not derive the public field allowlist automatically from every column in a database view.

```python
from grad_pylib.core.querying import QuerySpec, apply_query
from sqlalchemy import select

spec = QuerySpec(
    filterable={"status": items.c.status},
    sortable={"name": items.c.name, "id": items.c.id},
    default_sort="name,id",
)
stmt = select(items).where(items.c.term_code == term_code, access_condition)
stmt = apply_query(stmt, spec, filters=column_filters, sort=effective_sort)
stmt = stmt.offset(offset).limit(page_size)
```

`effective_sort` must include a unique tie-breaker even when the caller supplies a sort.
Do not repeat a column already requested. Give list, count, and export queries the same
authorization and filtering predicates. Export-specific privacy exclusions may be stricter,
but must be deliberate. SQL Server paging needs ordering; validate offsets and sizes.

Distinguish `None` meaning "no optional filter" from an empty authorized-ID collection meaning
"no access." An empty scope must not remove the scope predicate. Define whether search text
is literal or a LIKE pattern; use `escape_like()` and the matching SQL escape character for
literal substring searches.

For SQL batches, reuse `where_fragment()` and `order_by_fragment()` with the same
`QuerySpec`, instead of building another filter language in strings. See [QUERYING.md](QUERYING.md).

Page rows plus total count can be two rowsets in one round trip. Validate that the count
rowset contains its required row; a missing count is not silently zero. One batch does not
make two SELECTs observe one snapshot under RCSI. If exact agreement is required, choose an
appropriate transaction-level snapshot or materialize the authorized candidate set once.
Changing isolation requires explicit configuration and cleanup, not an ad hoc `SET` in a query.

## SQL text and parameterization

Use SQL text for a clearer set-based operation, SQL Server-specific features, or a justified
multi-statement round trip. Do not replace straightforward ORM/Core lookups with a batch
merely to use this facility.

### Values and structural SQL

```python
from sqlalchemy import tstring

stmt = tstring(t"""
    SELECT submission_id, term_code
    FROM dbo.student_submissions
    WHERE term_code = {term_code}
      AND {StudentSubmission.submission_id.in_(submission_ids)}
""")
```

With `tstring`, ordinary interpolated values become parameters, while SQLAlchemy expressions
and composed `TString` fragments become SQL. This is not equivalent to `text(f"...")`.
Do not preformat values into strings, add quotes around value interpolations, or convert
untrusted text into `text()`/`literal_column()` to make it render as SQL.

Table names, column names, operators, and sort direction cannot be value parameters. Resolve
them through explicit developer-owned mappings to SQLAlchemy tables/columns or fixed
fragments. Validate table/column pairs before looking them up in metadata; a syntactically
valid identifier is not necessarily an allowed update target.

Keep `text("... WHERE code = :code")` plus a parameter dictionary for existing static SQL and
loaded SQL files. Use expanding binds for variable-length `IN` lists, never quoted CSV.
`exec_driver_sql()` is for driver-level tooling with driver-specific parameter syntax, not
the default application-query API. `GO` is a script batch separator, not executable T-SQL.

### SQL Server types and collection inputs

Python strings can arrive at SQL Server as Unicode parameters. For indexed `varchar`
predicates, a column-side implicit conversion can change seekability under some collations.
Prefer correctly typed schema expressions/binds; when a measured plan requires it, assign
bound values to validated typed locals or cast the **parameter**, not the indexed column.
Do not apply blanket non-Unicode binding or copy arbitrary local-variable widths: conversion
can lose characters or silently truncate values and match the wrong record.
For typed locals, validate length/encoding before assignment; declaring a narrower SQL type
is not input validation.

Keep strings such as UINs and term codes as strings when the schema does. Use `Decimal` for
exact numeric columns, preserve `NULL` versus empty string, and define timezone handling at
the boundary. SQL Server `datetime2` does not store a timezone; a UTC convention is still
necessary. Trimming, case folding, and NULL normalization must match the field's domain and
database comparison semantics, not a universal "clean every value" helper.

Small collections use `.in_(values)`. For large collections, account for SQL Server's
2,100-parameter limit across the **whole statement**, including repeated and expanded binds.
Use bounded chunks when independent work can be chunked, or a bound JSON payload with typed
`OPENJSON ... WITH (...)` for a set-oriented input. Table-valued parameters or temporary staging
tables are specialized alternatives, not necessary infrastructure for every query.

Validate collection element types, duplicate keys, and payload size. Bind the JSON as data.
Do not accidentally turn an atomic operation into independently committed chunks.

## Batch execution and result sets

**Target contract; availability is tracked in [DB-IMPLEMENTATION.md](DB-IMPLEMENTATION.md).**

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
Support it for deliberate textual SQL Server batches through the app's configured engine.

### One final rowset

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

### Multiple rowsets

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

Without the option, non-row results continue to appear as empty lists for compatibility. Ordinary
single-rowset mapping helpers, nested DTO mapping, and SQL column helpers need no new modes.

### Batches with no rowsets

Use the same execution option for a DML-only/control-flow batch. Execution completes only
after all non-row results have been traversed, surfacing any errors encountered. Close the
returned result, but do not fetch rows from it or pass its absent cursor to the
multi-result reader. If an expected DML-only batch returns columns, reject the unexpected
shape or deliberately use full traversal; do not silently close at that first rowset.

Do not use the final `result.rowcount` as the total effect of a batch. When correctness
depends on affected rows, return explicit affected keys, counts, or an outcome SELECT.
Capture `@@ROWCOUNT` immediately after the DML it describes, not after intervening statements.

### Error and connection contract

Under the target contract, database failures during execution or result consumption surface
as SQLAlchemy `DBAPIError` instances with the original driver diagnostics preserved.
Disconnected connections are invalidated so the existing explicit retry/error policies can
handle them. Application services must not wrap raw driver errors or reach into SQLAlchemy
private methods themselves; library integration is covered in [DB-IMPLEMENTATION.md](DB-IMPLEMENTATION.md).

Do not suppress a later error because earlier rowsets were valid, or return partially collected
data as success. The reader closes its result on failure; let the transaction owner roll back.

Handling row-count results removes the need to mutate `NOCOUNT` for these patterns. It does
not solve arbitrary session-state changes. Pool rollback is not a complete reset of SET
options or committed temp tables. Prefer batch-scoped table variables; isolate exceptional
session-state changes and restore them explicitly. A pool reset is a fallback on return,
not cleanup before the next statement on the same checked-out connection. It can also clear
intentional connection initialization, which then needs restoration.

## Transactions and retries

**The calling code chooses the pattern.** There is no implicit retry or commit based on the
HTTP verb, SQL text, or session dependency. A transaction-owning service operation may call
helpers that flush, but those helpers must not commit, roll back, or start an independent
transaction behind its back.

### Plain writes

Use an explicit transaction when the operation should be attempted once:

```python
def create_item(session: Session, body: CreateItem) -> ItemDto:
    with session.begin():
        item = Item(name=body.name)
        session.add(item)
        session.flush()
        dto = ItemDto.model_validate(item)
    return dto
```

The context commits on normal exit and rolls back on exceptions. Return a materialized
result, not an open cursor or lazy iterator. Map/validate the response inside the transaction
when a mapping failure should prevent commit. Do not report success before the context exits.

### Retrying writes

Choose `retry_on_transient_conflict` when the **whole database operation** can safely replay:

```python
from grad_pylib.core.db import retry_on_transient_conflict

@retry_on_transient_conflict
def create_item_with_retry(session: Session, body: CreateItem) -> ItemDto:
    item = Item(name=body.name)
    session.add(item)
    session.flush()
    return ItemDto.model_validate(item)
```

The decorator owns begin/commit/rollback. It receives exactly one session, and that session
must have no active transaction. Put all decision-making reads and writes inside the
decorated function. Do not call `commit()` or another decorated transaction owner inside it.
Factor reusable non-owning helpers out when operations must compose atomically.

The existing policy makes up to three attempts for deadlocks, lock timeouts, native error
3960, and qualifying lost connections, with short waits. It does not retry validation
failures, optimistic conflicts, uniqueness violations, or every `DBAPIError`.

A transaction replay is different from replaying a client request. Do not perform HTTP calls,
send messages, publish cache entries, or mutate reusable input/plan objects inside the retried
body. Build attempt-local ORM objects and outputs again on each attempt. Keep the request's
idempotency key stable.

### Autobegin and composing work

Even a SELECT can autobegin a transaction. Both `session.begin()` and the retry decorator
require a deliberate boundary before they take ownership. Do not scatter `rollback()` calls
to silence "already begun" errors: that can discard someone else's pending writes.

First remove earlier reads whose existence/condition checks the mutation can perform itself.
If a necessary earlier read influences a write, move it inside the owning operation and
protect its decision against concurrent change. If an independent preflight read is needed,
finish its own scope, carry plain values forward, and enforce mutable assumptions in the
write predicate or batch rather than automatically adding another validation query.
Read-only auth dependencies must not leave a shared request session in a transaction; the
shared role-override loader handles its own read boundary. See [AUTH.md](AUTH.md).

When an operation is caller-owned, document that it neither begins nor commits a transaction.
Use `begin_nested()` only for a deliberately recoverable sub-operation, not to disguise an
unknown transaction owner. Savepoints do not make a deadlock-aborted transaction recoverable.
With SQL Server error/SET combinations that doom a transaction, roll back the whole operation.

### Retrying reads and ambiguous commits

For an explicitly retryable read endpoint, place `@retry_on_disconnect` below the route
decorator. It retries once, first rolling back session arguments so they can reconnect.
Never apply it to an endpoint that writes, even if most of the endpoint is a read.
Dependency failures happen before the decorated endpoint and are not retried by it.

```python
from grad_pylib.core.db import retry_on_disconnect

@router.get("/items/{item_id}")
@retry_on_disconnect
def get_item(item_id: int, session: DbSession, user: UserPolicy) -> ItemDto:
    return read_authorized_item(session, item_id, user)
```

The same choice can be made at a service/background read boundary; do not stack retry
decorators at multiple layers. A generator whose database work starts after the decorated
function returns is not covered.

Do not retry a lost connection during commit automatically: the commit may have succeeded.
The current pyodbc classification uses its driver diagnostics to distinguish this case.
A session that SQL Server terminates mid-request (an admin `KILL` or a severity 20+ error) is
also treated as a lost connection, because the server rolls back its work.
Its safety assumptions require ordinary transactional writes: no internal SQL commits,
autocommit, or external side effects inside the supposedly replayable unit.

Register the shared exception handlers to expose unrecovered disconnects as HTTP 503 with
`Retry-After`. A 503 on a write is **not** evidence that the write was rolled back; clients
need an idempotency/reconciliation contract before resubmitting it.

## Concurrency, audit, and idempotency

### Optimistic edits

For a user's "change this value from X to Y" operation, put the expected value and scope in
the UPDATE/DELETE predicate. A separate unlocked read followed by an unconditional write
does not implement optimistic concurrency.

```python
from grad_pylib.core.exceptions import ConflictError
from sqlalchemy import update

def apply_name_change(session: Session, item_id: int, was: str | None, to: str) -> int:
    changed_id = session.execute(
        update(Item)
        .where(Item.id == item_id, Item.name == was)
        .values(name=to)
        .returning(Item.id)
    ).scalar_one_or_none()
    if changed_id is None:
        raise ConflictError("The record changed or is no longer available.")
    return changed_id
```

This helper participates in its caller's transaction. Add authorization and domain-state
predicates as required. SQLAlchemy column comparisons handle `None` as `IS NULL`; textual
SQL must express the NULL-safe comparison explicitly. Do not conflate absent optional rows,
NULL fields, empty strings, false, and zero unless that is the field's documented contract.
There is intentionally no `session.get()` before the UPDATE. Do not add one solely to
distinguish "missing" from "changed"; the combined conflict is sufficient unless the endpoint
requires a distinction that can be diagnosed simply on the failure path.

For ordinary DML without OUTPUT, a known, reliable `rowcount` may be checked. With OUTPUT,
batches, triggers, or unknown counts, verify affected keys or an explicit outcome instead.
Do not assume `-1` means zero changes. Check all expectations before commit.

Use 409 Conflict as the recommendation for stale client expectations; use 404 for an
established missing-resource contract. Preserve an existing endpoint's documented distinction
until intentionally changing it.

### Optional state, overrides, and JSON fields

An absent related row can represent "no override" or "not confirmed," but only by an explicit
domain rule. Separate the commands "leave unchanged," "set a value," and "clear the override."
If clearing reveals upstream data, it is not the same as setting the effective value to NULL.
Read-only effective-data views are projections; write to the owning source/override tables.

Enforce owner existence and the optional-key decision in conditional DML/constraints where
possible, protecting absent-key races when necessary. Database-defined conditions, such as
an active workflow-check key, belong in the write's predicate/join or batch; a separate
validation SELECT is not the default. For natural-key get-or-create, enforce uniqueness and
use a conditional insert with appropriate concurrency protection, a protected lookup/insert
when the row's data is needed, or known-constraint savepoint recovery. Only reread the winning
row after the failed insert has been rolled back and the surrounding transaction is usable.

For editable JSON, allowlist/validate keys and value types. Prefer a field-level SQL update
such as `JSON_MODIFY` with a bound path/value rather than overwriting an entire stale document.
Define the distinction between an absent property and JSON null: SQL Server's lax
`JSON_MODIFY(..., NULL)` removes a property. Apply the same optimistic comparison and audit
rules as ordinary columns; do not convert malformed JSON into an empty successful value.

### Deletes, merges, and relationship maintenance

Decide explicitly between hard deletion, soft deletion, and unlinking. State what happens
to dependent rows, audit/history, unique keys, and subsequent repeated requests. Do not
introduce a generic cascade or discard history merely to make a foreign-key error disappear.

For a merge or reassignment, validate and protect the source and target, resolve duplicate
relationships according to a documented rule, move the related data, retire the source,
and write audit records in one transaction. Keep creation of required companion rows in
that same transaction. Prefer mutation predicates and constraints to separate existence or
duplicate checks. Read source/target data only when the merge decision actually needs it;
perform additional error diagnosis on failure rather than charging every success for it.

For reorderings or permutations under unique constraints, account for intermediate states:
several separate updates may collide even if the final assignment is valid. Use a reviewed
set-based strategy or deliberate delete/flush/recreate sequence inside the transaction.
Check affected keys and preserve relation identity/history where the domain requires it.

### Locking and upserts

Prefer conditional DML and constraints over selecting and locking rows for validation in
Python. Explicit locking is for invariants that cannot be enforced adequately that way,
such as a protected absent-key decision or a shared activity marker.

**Locking more than one or two rows in a table is an extraordinary case**, not the normal
shape of a request. Target indexed keys narrowly; do not pre-lock a term, collection, or
table merely to simplify application logic. An apparently single-row query can take wider
range/page locks or scan many keys, so examine its access path rather than counting only
returned rows. This is a lock-footprint design target, not a literal limit on SQL Server's
internal lock objects; a `ROWLOCK` hint does not make a broad operation acceptable.

Ideally the lock-dependent work happens in one understandable server-side batch, without
intervening client round trips. Locks generally remain held until the transaction ends,
even after the batch returns: consume its results, do only necessary remaining work, and
commit promptly. Do not perform network lookups, unrelated queries, or expensive formatting
while holding them. Keep multiple readable steps when combining them would obscure the
logic, but make the extra locked round trips deliberate and bounded.

`orm_upsert()` is the standard ORM primary-key upsert when that pattern is appropriate:
supply the complete mapped primary key, mutable fields, and optional `insert_only` fields.
It takes SQL Server update/range locks, reads the row, then flushes, leaving commit to the
caller. That readable multi-call pattern is a tradeoff, not a reason to use it for every
UPDATE or to call it repeatedly over a large collection. Prefer direct conditional DML when
no ORM read is needed. It is not an arbitrary unique-key merge, not an optimistic edit, and
not an idempotency policy; do not default to a generic MERGE string either.

When a narrow locking read really is required, SQLAlchemy `with_for_update()` does not
provide the needed SQL Server locking clause. Use an explicit SQL Server hint:

```python
locked = session.scalars(
    select(Item)
    .where(Item.id == item_id)
    .with_hint(Item, "WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql")
    .execution_options(populate_existing=True)
).one_or_none()
```

Use `populate_existing` when already-loaded ORM state must reflect the locked read, and
flush intentional pending changes first. Do not overwrite pending changes accidentally.
Do not trust the identity map after textual writes; refresh/expire affected objects or
return explicit projections.

All competing mutation paths must follow a compatible lock protocol and table/key ordering.
Lock hints protect only what their query actually locks. A cooperative parent-row lock or
`sp_getapplock` is useful only if every relevant writer participates. Check application-lock
return codes; a negative result is not success. SQL Server `ORDER BY` alone is not a guarantee
of physical lock acquisition order. Review access paths and keep contention recovery bounded.

READ COMMITTED SNAPSHOT reduces read/write blocking; it does not serialize business
decisions, provide one snapshot across a multi-SELECT batch, or remove write/write conflicts.
Error 3960 is an update conflict associated with snapshot isolation; the helper's historical
`RCSI_CONFLICT` name is not a reason to treat all RCSI reads as serializable.

### Audit and replay

Write the business mutation, its audit entries, and authoritative activity/read markers in
one transaction. A failed operation must not leave a committed audit row or a marker for work
that never committed. Read a required post-write snapshot inside that transaction, rather
than committing and then relying on a follow-up query to establish success.

Recommended request replay contract: the same scoped idempotency key and same payload do
not perform the mutation twice; return the original result or a documented equivalent.
Reusing the key for different input should conflict. Define the key's scope, payload
comparison, result retention, and no-op behavior explicitly. A status returned from the
current state is not automatically equivalent to the original result.

Enforce replay identity with a database uniqueness constraint or an equally strong locked
protocol, not just a preflight SELECT. An audit table may serve this purpose only if its
key/payload contract really identifies the operation. Otherwise use an operation record.
Audit rows for individual entities do not necessarily prove that an entire bulk request ran.

Recognize only the expected duplicate-key constraint/index. An unrelated duplicate may mean
a seat is occupied, an email belongs to someone else, or an invariant failed. Catch replay
errors **outside** the transaction boundary, after rollback; suppressing them inside that
boundary can commit partial work. Resolve a possible recorded replay before reporting a stale
`was` value as a fresh conflict. That does not require a replay SELECT on every success:
where the operation's protocol permits, use the unique-key outcome or a failure-only lookup.

Some operations deliberately reject repeated runs; others, such as webhook redelivery,
return a no-op success. These are valid explicit exceptions. Do not inherit whichever
behavior happens to fall out of a broad integrity-error catch.

### Bulk writes

Recommend **atomic all-or-nothing** behavior unless the product explicitly calls for partial
success. Validate the input, encode expected values and scope in the mutation, apply changes,
record audit entries, verify the affected set, and commit once. Conflicts should identify the
relevant records without exposing unauthorized data.

Use set-based DML or batched ORM/Core operations rather than one round trip per field or
row. Do not fetch and lock the entire candidate set merely to compare old values in Python
when conditional DML can do that comparison. Return changed keys and compare them with the
intended set; a mismatch rolls back all changes and audit rows. Fetch current conflict values
only on failure when they are useful and straightforward to obtain. Validate duplicate input
keys and keep a deterministic order for diagnostics and cooperative locking.

A genuine bulk change necessarily locks more than one or two rows and is therefore an
explicit exception to the normal locking target. Document why that scope is needed, bound
the operation, and minimize locked duration and client round trips. Do not use term-wide
pre-locking as the template for ordinary edits, or split an atomic operation into separately
committed rows just to appear to meet the locking target.

If partial success is needed, explicitly define which records can fail independently, the
per-record outcome shape, and replay behavior. One transaction may commit its accepted
subset, but infrastructure/constraint failures must not silently become "updated zero,
no conflicts." Do not introduce commits in a loop or infer partial-success policy from the
current hooding bulk implementation.

For monotonic watermarks, compute the new value under the write's concurrency protection
using max-wins semantics where the domain requires them. A read-then-assign in Python can
move a marker backward when concurrent requests commit out of order.

## Errors, side effects, and long-running work

### Error translation

Parse SQL Server errors with `parse_mssql_error()`, preserving the original exception chain.
Translate known domain constraints outside the transaction owner; leave transient errors
intact until the retry boundary has processed them.

| Failure | Recommended handling |
|---|---|
| Known uniqueness or optimistic conflict | 409, or the explicitly documented replay outcome |
| Known input-related FK/check/type violation | Domain-specific 400/404/409 as appropriate |
| Deadlock, timeout, snapshot update conflict | Retry only at an opted-in whole-transaction boundary |
| Lost connection | Shared disconnect handling; never assume a commit failed |
| Invalid SQL, missing schema, permissions, unknown error | Propagate as an operational/programming error, not "bad input" |

Do not catch every `IntegrityError`/`DBAPIError` and report success or a generic validation
failure. A classification such as `is_data_validation_error` is a mechanism, not proof that
the client caused the error.

### Failure-only diagnostics

Prefer the statement's affected keys/count or recognized constraint error as the source of
the outcome. If these establish failure but do not distinguish missing, stale, or conflicting
data, a small diagnostic query is acceptable when the distinction materially helps the caller.
Do not build a complex diagnostic subsystem or spend successful-path queries on error wording.

After a database exception, let the transaction owner roll back before querying again, unless
an intentionally recoverable savepoint has restored a usable transaction. After zero-row or
partial-match DML, still ensure any other writes/audit entries are rolled back; a diagnostic
must not turn the operation into a partial commit. Never roll back a caller-owned transaction
from a low-level helper just to issue a diagnostic.

A later read observes current state, not necessarily the exact reason the earlier statement
failed. Keep it authorized, tolerate concurrent changes, and fall back to the established
general conflict if no precise conclusion is supported. Do not diagnose infrastructure
failures as missing records or obscure the original error if the diagnostic query itself fails.

### Logging

Use structured operation/retry/disconnect logging. Avoid credentials, connection strings,
SQL parameters, and full DTOs in logs. Driver messages and exception tracebacks may themselves
contain values; review redaction rather than assuming a diagnostic is safe.

### Caches and post-commit work

Cache plain immutable values or DTO snapshots, never live sessions, results, or ORM graphs.
Check authorization on cache hits as well as misses. Authorization-sensitive cache keys and
invalidation must reflect effective scopes/overrides.

Invalidate only after a successful commit, outside any retry loop. Do not fill a shared cache
from uncommitted writes. Guard against an in-flight stale fill undoing an invalidation.
Local in-memory invalidation affects only one process; multi-replica or external-writer
correctness needs a shared version/invalidation mechanism or an explicitly acceptable TTL.

For polling, choose a marker that changes for every relevant mutation, including deletes
that do not change the maximum ID. An ETag must also reflect representation changes.
Post-commit cache/response failures do not undo a write; do not replay the write to repair them.

External side effects requiring reliable delivery belong in an outbox/work record written in
the same transaction as the mutation. An after-commit HTTP call alone is not atomic delivery.
For simple best-effort work, state that contract and make failures visible.

### External lookups and ingestion

Prepare network-derived data outside the write transaction. If necessary to obtain inputs or
avoid expensive external work, perform a preflight read, materialize plain inputs, and release
that read scope before network I/O. In the write phase, enforce mutable ownership/state in the
mutation or read/protect it only when needed to compute the change. Apply the changes and
enqueue durable follow-up work together. Pass identifiers/data across phases, not a live
cursor or session-bound graph.

For incoming webhooks, define delivery identity and make creation plus deduplication atomic.
Redelivery must not overwrite later administrator edits unless the domain explicitly says
it should. Negative application-lock outcomes, enrichment failures, and invalid external
identities need explicit outcomes, not successful defaults.

### Background jobs and backfills

```python
def run_job(item_id: int) -> None:
    with get_background_session() as session:
        with session.begin():
            apply_job_change(session, item_id)
```

Alternatively call a retry-decorated owner inside the background-session context without
the outer `session.begin()`. The choice remains explicit. A background task receives IDs or
plain values, never the request's session.

A backfill separates planning from application. A dry run must not insert/flush and then
claim that rollback made it read-only: identity allocation and side effects may still occur.
Revalidate the plan under concurrency protection during application; do not trust stale ORM
objects. Choose and document all-or-nothing versus checkpointed chunks, resumability, and
replay behavior. Confirm the target database for destructive maintenance.

### Exports and streaming

Use the same authorized base query as the list endpoint; load child collections in bounded
sets rather than per row. For bounded exports, materialize data before building the response.
A `StreamingResponse` over an in-memory buffer does not require a database cursor to remain open.

For genuinely large exports, explicitly own the session/cursor for the generator's lifetime,
bound memory, and close resources on disconnect/error. Do not assume a route-level retry
decorator can replay a partially transmitted stream. Streaming capability depends on the
driver; verify it rather than treating `yield_per` as a universal guarantee.

## Schema and verification

### Migrations and generated models

SQL migrations are the schema source of truth. Use the project's migration runner; do not
add `metadata.create_all()` to application startup or hand-edit generated models.
The runner records filenames and checksums, and applies a file's batches in a transaction.
Applied table/data migrations are immutable. Follow the project's view-migration convention:
hooding renumbers/reapplies changed view definitions and their dependencies in valid order.
Do not teach an agent to modify an already-applied filename just because a view is involved.

Regenerate models using the project's `make generate-models` or `make rebuild-models`
workflow and retain model-parity coverage. Keep model-generation configuration responsible
for reflection quirks; keep feature behavior in services rather than generated classes.
Administrative `CREATE/ALTER DATABASE` and autocommit operations are separate tooling paths,
not ordinary retryable request transactions.

Schema scripts and provisioning code must also surface errors from every batch result.
Do not apply the request hook blindly to tools that use other drivers or autocommit; give
their executor an explicit full-result-draining contract.

### Required coverage for a chosen pattern

Use real SQL Server integration tests for SQL Server semantics, with the shared bootstrap,
migrations, Codebook fixture, and session dependency overrides. Keep fixture runtime settings
and engine hooks aligned with production. Do not use SQLite as a substitute. Pure mapping
and query-composition tests may remain database-free; unit doubles are not proof of lock,
OUTPUT, JSON, or ODBC behavior. The removal of existing SQLite tests is tracked in
[DB-IMPLEMENTATION.md](DB-IMPLEMENTATION.md).

| Pattern | Behaviors to cover |
|---|---|
| Optional/singleton read | Missing row, unexpected duplicates, NULL versus absence |
| Joined/multi-result DTO | Missing children, empty rowsets, fixed set count/order, alias collisions |
| Batch option | Leading/intervening/trailing row counts, zero-row DML, no-rowset batches, empty SELECT |
| Batch failures | Execute/fetch/nextset errors, later-statement failure, closure, rollback, disconnect classification |
| Plain/retryable write | Commit/rollback boundaries, retries only when chosen, complete-attempt replay, no retry on ambiguous commit |
| Conditional/audited write | Stale `was`, NULL expectations, no partial audit, same-key replay and different-payload conflict |
| Locks/upserts/bulk work | Concurrent sessions, invariant enforcement, intended atomicity, duplicate input, deadlock recovery |
| Call/lock budget | No redundant successful-path checks, failure-only diagnosis, narrow indexed lock scope, justified bulk exceptions |
| Reads/counts/exports | Shared authorization/filtering, stable sorting, paging boundaries, required consistency |
| Caches/external work | Commit-before-invalidation, failed attempts, stale fills, multiple writers, no repeated external side effects |

Use separate connections/sessions and controlled synchronization for concurrency tests.
Do not simulate contention by sharing a session between threads. Keep real constraints and
RCSI/isolation configuration representative. Tests that commit need cleanup of committed
rows; a final rollback alone is insufficient. Reset global caches between tests.

Preserve measured query-plan and round-trip requirements where they justify SQL complexity.
Test meaningful plan properties with realistic binding/collation and a negative control,
not an exact optimizer plan for all environments. Index/FORCESEEK hints need evidence;
`NOLOCK` is not a correctness-preserving performance shortcut.
Count success-path calls separately from failure diagnostics. For lock-sensitive operations,
check how many calls occur after the first lock-taking statement and before commit; for an
exceptional broad mutation, cover realistic contention and lock scope rather than just the
number of rows returned.

Run the smallest relevant commands with `uv run pytest ...`; use the projects' existing
tooling for models/migrations. Never run migrations or backfills against an arbitrary
configured database merely to validate a code change.

## Implementation checklist for an agent

- Identify the pattern and confirm unresolved domain choices before coding.
- Use configured dependencies and explicit transaction ownership; keep helpers composable.
- Prefer ORM/Core, then bound `tstring` SQL where it is clearer or measurably useful.
- Remove redundant existence/condition queries; prefer fewer success-path calls over precise error wording.
- Keep scope predicates, cardinality, concurrency checks, and rowset contracts visible.
- Keep locks narrow and short-lived; justify more than one or two rows per table and minimize locked round trips without obscuring the code.
- Consume and validate required results before commit; do not hide later driver errors.
- Choose retries explicitly and preserve a stable request identity across attempts.
- Keep business changes, audit, replay records, and required follow-up state atomic.
- Separate external I/O and post-commit work from the replayable database operation.
- Preserve endpoint contracts; document and cover intentional exceptions.
- Verify the relevant SQL Server behavior, not just the shape of mocked calls.

## Pattern inventory

The Python services in these repositories were examined to identify the needed patterns.
These paths are examples of use cases, **not instructions to copy their current mechanics**.
The legacy .NET code in grad-codebook is not the target application architecture.

| Repository and representative paths | Pattern represented |
|---|---|
| All three: `core/db.py` (under `src/grad_codebook` for Codebook) | Named databases, FastAPI dependencies, background sessions |
| grad-codebook: `features/subjects`, `concentrations`, `program_codes`, `department_codes` | Optional and list reference-data DTOs, collection filters |
| grad-codebook: `features/programs`, `departments`, `minors` | Joined nested DTOs, aliases/column metadata, positional legacy mapping |
| gcp-server: `features/search`, `terms/graduations`, `terms/lookups` | Scoped multi-database search, shared list/export filters, page/count and lookup batches |
| gcp-server: `features/graduations/detail`, `updates`, `last_read` | Named multi-rowset contracts, typed parameters, monotonic markers |
| gcp-server: `features/graduations/write`, `statuses`, `comments` | Conditional writes, authoritative post-write response, audit, lock ordering |
| gcp-server: `features/config`, `programs`, `users`, `comments/cache.py` | Primary-key upserts, authorization caches, polling markers, invalidation |
| hooding-server: `features/students/service.py`, `hooders/service.py` | Aggregate details, generated views, relation creation, soft deletion, merges |
| hooding-server: `core/updates.py`, student/hooder update services | Allowlisted field edits, optional related rows, audit and OCC |
| hooding-server: `features/terms/seating_service.py`, `ceremony_service.py` | OUTPUT batches, reservations, swaps, ordered recomputation, operation replay |
| hooding-server: `features/students/bulk_update_service.py`, `workflow_check_service.py` | Set-oriented edits, JSON fields, conflict reporting, dynamic domain keys |
| hooding-server: `features/tags/service.py` | Natural-key creation races, savepoints, assignment constraints |
| hooding-server: `features/students/hooder_link_service.py`, `webhooks`, `enrichment` | External lookups, revalidation, deduplicated ingestion, durable work state |
| hooding-server: `features/hooders/backfill_service.py`, `src/backfill_hooders.py` | Read-only planning, apply-time revalidation, maintenance scopes |
| gcp-server and hooding-server: export services | Authorized projections, bounded child reads, response buffering |
| All three: `conftest.py`, model-parity tests, schema/tooling | SQL Server fixtures, cross-database reference data, migrations, generated models |

### Technical references

- [SQLAlchemy: multiple result sets](https://docs.sqlalchemy.org/en/21/core/connections.html#multiple-result-sets)
  documents raw-cursor traversal, not a built-in `mssql_skip_rowcounts` option.
- [Microsoft: SQLMoreResults](https://learn.microsoft.com/en-us/sql/odbc/reference/syntax/sqlmoreresults-function#comments)
  documents advancing over row counts and the possibility of errors during traversal.
- [SQLAlchemy: SQL Server pool resets](https://docs.sqlalchemy.org/en/21/dialects/mssql.html#temporary-table-resource-reset-for-connection-pooling)
  distinguishes transaction rollback from wider session-state cleanup.
- [SQLAlchemy: pyodbc pooling](https://docs.sqlalchemy.org/en/21/dialects/mssql.html#pyodbc-pooling-connection-close-behavior)
  recommends disabling pyodbc's module-level pooling before any connection is made.
