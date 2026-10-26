# Errors, side effects, and long-running work

## Error translation

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

## Failure-only diagnostics

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

## Logging

Use structured operation/retry/disconnect logging. Avoid credentials, connection strings,
SQL parameters, and full DTOs in logs. Driver messages and exception tracebacks may themselves
contain values; review redaction rather than assuming a diagnostic is safe.

## Caches and post-commit work

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

## External lookups and ingestion

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

## Background jobs and backfills

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

## Exports and streaming

Use the same authorized base query as the list endpoint; load child collections in bounded
sets rather than per row. For bounded exports, materialize data before building the response.
A `StreamingResponse` over an in-memory buffer does not require a database cursor to remain open.

For genuinely large exports, explicitly own the session/cursor for the generator's lifetime,
bound memory, and close resources on disconnect/error. Do not assume a route-level retry
decorator can replay a partially transmitted stream. Streaming capability depends on the
driver; verify it rather than treating `yield_per` as a universal guarantee.
