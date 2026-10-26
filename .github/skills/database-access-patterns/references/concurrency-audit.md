# Concurrency, audit, and idempotency

## Optimistic edits

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

## Optional state, overrides, and JSON fields

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

## Deletes, merges, and relationship maintenance

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

## Locking and upserts

Prefer conditional DML and constraints over selecting and locking rows for validation in
Python. Explicit locking is for invariants that cannot be enforced adequately that way,
such as a protected absent-key decision or a shared activity marker.

Scope locking reads to the keys or ranges needed for the invariant. Avoid pre-locking an
entire term, collection, or table to update a few rows merely because it simplifies a
preflight check. An apparently single-row query can still scan many keys or take wider
range/page locks, so consider its predicates, indexes, and access path rather than only its
result count. There is no numeric row limit: broader operations may need broader locks.
A `ROWLOCK` hint does not fix an unnecessarily broad query.

Ideally the lock-dependent work happens in one understandable server-side batch, without
intervening client round trips. Locks generally remain held until the transaction ends,
even after the batch returns: consume its results, do only necessary remaining work, and
commit promptly. Do not perform network lookups, unrelated queries, or expensive formatting
while holding them. Keep multiple readable steps when combining them would obscure the
logic, but make the extra locked round trips deliberate and bounded.

`orm_upsert()` is the standard ORM primary-key upsert when that pattern is appropriate:
supply the complete primary key and mutable fields in `data_source`, with optional
`insert_only` fields that apply only when creating a row:

```python
from grad_pylib.core.db import orm_upsert

item = orm_upsert(
    session,
    Item,
    {"id": item_id, "name": new_name},
    insert_only={"created_by": actor_id},
)
```

Run this inside the caller's transaction. Here `id`, `name`, and `created_by` are mapped
**ORM attribute names**, which may differ from physical database column names. Unrecognized
keys are ignored by the helper, so construct the payload explicitly rather than passing
arbitrary request fields. Include every component of a composite primary key in
`data_source`, not `insert_only`. Overlapping mapped keys in the two payloads raise
`ValueError`. On update, omitted fields stay unchanged; explicit `None` assigns NULL where
allowed. Pydantic inputs use `model_dump(exclude_unset=True)`, preserving unset fields.

The helper flushes pending session changes, takes SQL Server update/range locks, refreshes the row
even if it is already loaded, then flushes the upsert, leaving commit to the caller. The
initial flush preserves pending edits before the refresh replaces stale identity-map state.
Finish and validate those edits before calling the helper: their constraints and triggers
can run at that initial flush, even with `autoflush=False`. That readable multi-call pattern
is a tradeoff, not a reason to use it for every UPDATE or repeatedly over a large collection.
Prefer direct conditional DML when no ORM read is needed. It is not an arbitrary unique-key
merge, not an optimistic edit, and not an idempotency policy; do not default to a generic
MERGE string either.

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
`NOLOCK` is not a correctness-preserving shortcut for avoiding blocking.
Error 3960 is an update conflict associated with snapshot isolation; the helper's historical
`RCSI_CONFLICT` name is not a reason to treat all RCSI reads as serializable.

## Audit and replay

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

## Bulk writes

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

Bulk changes naturally need a wider lock scope than single-record edits. Keep that scope
aligned with the actual mutation and its invariants, bound the operation, and minimize
locked duration. Do not use term-wide pre-locking as the template for ordinary edits, or
split an atomic operation into separately committed rows merely to reduce lock scope.

If partial success is needed, explicitly define which records can fail independently, the
per-record outcome shape, and replay behavior. One transaction may commit its accepted
subset, but infrastructure/constraint failures must not silently become "updated zero,
no conflicts." Do not introduce commits in a loop or infer partial-success policy from
another endpoint.

For monotonic watermarks, compute the new value under the write's concurrency protection
using max-wins semantics where the domain requires them. A read-then-assign in Python can
move a marker backward when concurrent requests commit out of order.
