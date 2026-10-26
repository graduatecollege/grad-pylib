---
name: database-access-patterns
description: 'Choose and apply grad-pylib database-access patterns for synchronous SQLAlchemy + SQL Server (mssql+pyodbc). Use for database reads, writes, transactions, retries, batches, locking, migrations, fixture setup, and database-backed audit, cache, or export behavior.'
---

# Database access patterns

Apply these patterns to synchronous SQLAlchemy access to SQL Server through `mssql+pyodbc`
in Python 3.14/3.15 services using the **grad-pylib v8 database APIs and SQLAlchemy 2.1**.
FastAPI endpoints and dependencies are synchronous. Priorities, in order:
**correctness, ease of understanding, performance, maintainability**.

This folder is the canonical database-patterns guide, designed to be copied in full to
consuming projects without `DB.md`. Maintain shared guidance in the
[upstream skill](https://github.com/graduatecollege/grad-pylib/tree/main/.github/skills/database-access-patterns).
For older library versions, consult the
[migration guide](https://github.com/graduatecollege/grad-pylib/blob/main/MIGRATE.md)
before adopting newer APIs; an unconfigured engine silently ignores `mssql_skip_rowcounts`.

SQLAlchemy ORM/Core is the first choice for routine work; SQLAlchemy `tstring(t"...")` is the
preferred way to write new application SQL batches and fragments. Existing HTTP contracts,
authorization rules, and domain-specific normalization must not change as a side effect of
adopting a database helper. Explain intentional departures from a recommendation.
Examples use illustrative application models, DTOs, and service-local helpers, not
additional grad-pylib APIs.

## Workflow

1. **Identify** the database, schema objects, caller, and existing response contract. Check
   the consumer's dependency versions and engine setup before adopting a helper.
2. **Classify** the operation: read only, write once, or a complete write transaction that may
   safely replay. A POST can be read-only; a read-oriented service can still write.
3. **Choose the result shape** and consistency requirement: scalar, optional row, list, joined
   object, multiple rowsets, or a write outcome.
4. **Identify ownership and side effects**: transaction owner, concurrency guard, audit,
   replay, cache, external I/O. Consider successful-path calls and lock scope/duration,
   especially preflight reads that lock more data than the mutation needs.
5. **Select the simplest pattern** from the table below and make these choices visible in the
   calling code.
6. **Read the matching reference file(s)** below before writing code.
7. **Ask the user** only about choices relevant to the operation that existing contracts and
   requirements leave unresolved: snapshot consistency, conflict behavior, idempotency scope,
   partial vs. atomic success, bounded vs. streaming results, or replay safety.
   Do not invent retries or silently discard conflicts.

## Choosing a pattern

| Need | Preferred pattern | Reference |
|---|---|---|
| One entity or a short list | ORM/Core `select`, with explicit result cardinality | [reads](references/reads.md) |
| A read-only view or projected DTO | Core columns plus `.mappings()` and DTO validation | [reads](references/reads.md) |
| Nested objects from one join | Core tables/aliases plus `map_row_to_pydantic()` | [reads](references/reads.md) |
| Search, filtering, sorting, paging | `QuerySpec`, fixed authorization predicates, deterministic ordering | [reads](references/reads.md) |
| Several related collections or page plus count | One batch plus `read_all_result_sets()` | [batches](references/batches.md) |
| DML followed by one final rowset | Batch option plus ordinary SQLAlchemy result consumption | [batches](references/batches.md) |
| DML-only batch | Batch option; advance through all non-row results before reporting completion | [batches](references/batches.md) |
| One database write operation | An explicitly owned transaction, with or without retries | [transactions](references/transactions.md) |
| Edit only if a value is unchanged | Conditional DML plus verified affected keys/outcome | [concurrency-audit](references/concurrency-audit.md) |
| Insert or update supplied fields by complete primary key | `orm_upsert()` inside the caller's transaction | [concurrency-audit](references/concurrency-audit.md) |
| Optional state, overrides, or JSON edits | Explicit absence/clear semantics and protected field-level updates | [concurrency-audit](references/concurrency-audit.md) |
| Delete, merge, or reassign relationships | One transaction with explicit ownership, conflict, and history rules | [concurrency-audit](references/concurrency-audit.md) |
| Several records changed together | Atomic, set-oriented bulk operation; partial success only by explicit contract | [concurrency-audit](references/concurrency-audit.md) |
| External lookup followed by a write | Prepare outside the write transaction, then revalidate and apply | [errors-side-effects](references/errors-side-effects.md) |
| Cached read or polling marker | Plain-data cache, authorized access, post-commit invalidation | [errors-side-effects](references/errors-side-effects.md) |
| Backfill, export, or maintenance | Explicit session lifetime and transaction boundaries; no request session reuse | [errors-side-effects](references/errors-side-effects.md) |

## Reference files

Load only the files relevant to the task (paths relative to this skill folder):

| File | Read when |
|---|---|
| [bootstrap.md](references/bootstrap.md) | Setting up `NamedDatabases`/`DatabaseRuntime`, session dependencies, custom engines, pyodbc pooling, multiple databases |
| [reads.md](references/reads.md) | Result cardinality, not-found boundaries, joined/nested DTOs, `QuerySpec` filtering/sorting/paging, exports' base queries |
| [sql-text.md](references/sql-text.md) | Writing `tstring`/`text` SQL, structural vs. value interpolation, SQL Server types, `IN` lists, 2,100-parameter limit, `OPENJSON` |
| [batches.md](references/batches.md) | `mssql_skip_rowcounts`, final rowsets, `read_all_result_sets()`, DML-only batches, batch error/connection contract |
| [transactions.md](references/transactions.md) | `session.begin()`, `retry_on_transient_conflict`, autobegin, savepoints, `retry_on_disconnect`, ambiguous commits |
| [concurrency-audit.md](references/concurrency-audit.md) | Optimistic edits, optional/JSON fields, deletes/merges, locking hints, `orm_upsert()`, audit, idempotent replay, bulk writes |
| [errors-side-effects.md](references/errors-side-effects.md) | Error translation, failure-only diagnostics, logging, caches, external lookups/webhooks, background jobs, backfills, streaming exports |
| [schema-testing.md](references/schema-testing.md) | Migrations, generated models, `execute_script_batch()`, SQL Server fixture setup and cleanup |

## Core rules

### Successful-path calls

- Favor fewer database calls over more specific error messages. Do not query whether a row
  exists before an UPDATE/DELETE whose predicate and affected-row result already answer it.
- Use joins, conditional DML, `OUTPUT`, and constraints to enforce conditions instead of
  preflight queries. Required authorization and invariants must still be enforced.
- A general error such as "The record changed or is no longer available" is preferable to an
  extra query on every success. If a more specific diagnosis is useful and simple, query only
  after the statement fails or affects no expected rows, after the transaction owner has
  rolled back (see [Failure-only diagnostics](references/errors-side-effects.md#failure-only-diagnostics)).
- Reads needed to compute a change or produce a response are fine; prefer returning values
  from the mutation or its batch when that keeps code clear. Never trade readability for
  opaque SQL just to drop a round trip.

### Sessions and transactions

- One session per request/task; never module-global or used concurrently. Services
  receive a `Session` and never open a hidden second one. Background tasks get IDs/plain values.
- **The calling code owns the transaction.** No implicit commit or retry based on HTTP verb,
  SQL text, or dependency. Non-owning helpers may flush but leave transaction boundaries to
  the owner; an explicitly chosen operation or retry decorator owns begin/commit/rollback.
- Session teardown rolls back; it does not commit. Never move commits into dependency teardown.
- Sessions use `autoflush=False` and `expire_on_commit=False`: flush explicitly before
  dependent queries or reading generated IDs. `flush()` is not a commit.
- Retry only by explicit choice: `@retry_on_transient_conflict` for a whole replay-safe write
  (it owns begin/commit; no HTTP calls, messages, or cache publishes inside);
  `@retry_on_disconnect` only for read-only endpoints/boundaries. Never retry a lost commit.
- Two sessions are two transactions; cross-database writes need an explicit design.

### SQL and results

- Use the result method that matches expected cardinality (`scalar_one`, `one_or_none`, …);
  `.first()` only with intentional deterministic ordering and a SQL limit.
- With `tstring`, interpolated values become parameters; never `text(f"...")`, never quote
  value interpolations. Identifiers, operators, and sort direction come only from
  developer-owned mappings.
- Paging/sorting always ends with a unique tie-breaker. List, count, and export share the same
  authorization and filter predicates. An empty authorized-ID set means "no access", not
  "no filter".
- For batches, consume **all** results before commit; later errors may arrive only from
  `nextset()`. Do not use final `rowcount` as a batch total; return affected keys/outcomes.

### Concurrency, audit, and side effects

- Optimistic edits put the expected value and scope in the UPDATE/DELETE predicate and verify
  affected keys; stale expectations become 409 Conflict.
- Keep locks scoped to the mutation's invariants and short-lived, using indexed predicates.
  Avoid preflight queries that lock a whole term, collection, or table for a narrow change.
- Business change, audit rows, replay records, and required follow-up/outbox state commit
  atomically. Bulk writes are all-or-nothing unless partial success is explicitly specified.
- Enforce idempotency with a uniqueness constraint (or equally strong protocol), not a
  preflight SELECT; catch the expected duplicate key outside the transaction boundary.
- External I/O happens outside the write transaction; cache invalidation happens after a
  successful commit, outside retry loops.
- Translate only known SQL Server errors (`parse_mssql_error()`); never catch every
  `IntegrityError`/`DBAPIError` and report success or "bad input".
- Never log credentials, connection strings, SQL parameters, or full DTOs.

### Schema and tooling

- SQL migrations are the schema source of truth; applied migrations are immutable; never add
  `metadata.create_all()` to startup or hand-edit generated models.
- Never run migrations or backfills against an arbitrary configured database just to validate
  a change.
