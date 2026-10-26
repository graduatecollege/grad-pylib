# Transactions and retries

**The calling code chooses the pattern.** There is no implicit retry or commit based on the
HTTP verb, SQL text, or session dependency. A transaction-owning service operation may call
helpers that flush, but those helpers must not commit, roll back, or start an independent
transaction behind its back.

## Plain writes

Use an explicit transaction when the operation should be attempted once:

```python
from sqlalchemy.orm import Session

def add_item(session: Session, body: CreateItem) -> ItemDto:
    item = Item(name=body.name)
    session.add(item)
    session.flush()
    return ItemDto.model_validate(item, from_attributes=True)

def create_item(session: Session, body: CreateItem) -> ItemDto:
    with session.begin():
        dto = add_item(session, body)
    return dto
```

`add_item()` is non-owning: a caller can compose it with other helpers in one transaction.
`create_item()` owns the transaction and must not be called inside another owner's scope.
The context commits on normal exit and rolls back on exceptions. Return a materialized
result, not an open cursor or lazy iterator. Map/validate the response inside the transaction
when a mapping failure should prevent commit. Do not report success before the context exits.

## Retrying writes

Choose `retry_on_transient_conflict` when the **whole database operation** can safely replay:

```python
from grad_pylib.core.db import retry_on_transient_conflict

@retry_on_transient_conflict
def create_item_with_retry(session: Session, body: CreateItem) -> ItemDto:
    return add_item(session, body)
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

## Autobegin and composing work

Even a SELECT can autobegin a transaction. Both `session.begin()` and the retry decorator
require a deliberate boundary before they take ownership. Do not scatter `rollback()` calls
to silence "already begun" errors: that can discard someone else's pending writes.

First remove earlier reads whose existence/condition checks the mutation can perform itself.
If a necessary earlier read influences a write, move it inside the owning operation and
protect its decision against concurrent change. If an independent preflight read is needed,
finish its own scope, carry plain values forward, and enforce mutable assumptions in the
write predicate or batch rather than automatically adding another validation query.
Read-only auth dependencies must not leave a shared request session in a transaction; the
shared role-override loader handles its own read boundary. See grad-pylib's
[auth guide](https://github.com/graduatecollege/grad-pylib/blob/main/AUTH.md).

When an operation is caller-owned, document that it neither begins nor commits a transaction.
Use `begin_nested()` only for a deliberately recoverable sub-operation, not to disguise an
unknown transaction owner. Savepoints do not make a deadlock-aborted transaction recoverable.
With SQL Server error/SET combinations that doom a transaction, roll back the whole operation.

## Retrying reads and ambiguous commits

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
