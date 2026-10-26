# Reads and result mapping

## Routine reads

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

## Joined and nested DTOs

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

## Filtering, sorting, paging, and exports

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

`effective_sort` is prepared by the service and must include a unique tie-breaker even when
the caller supplies a sort. `QuerySpec.default_sort` is a fallback, not a suffix:
`apply_query()` and `order_by_fragment()` do not append it to a caller-supplied sort.
For this example, normalize `"name"` to `"name,id"`, but leave `"-id"` alone rather than
adding the same column again. Give list, count, and export queries the same
authorization and filtering predicates. Export-specific privacy exclusions may be stricter,
but must be deliberate. SQL Server paging needs ordering; validate offsets and sizes.

Distinguish `None` meaning "no optional filter" from an empty authorized-ID collection meaning
"no access." An empty scope must not remove the scope predicate. Define whether search text
is literal or a LIKE pattern; use `escape_like()` and the matching SQL escape character for
literal substring searches.

For SQL batches, reuse `where_fragment()` and `order_by_fragment()` with the same
`QuerySpec`, instead of building another filter language in strings. See grad-pylib's
[querying guide](https://github.com/graduatecollege/grad-pylib/blob/main/QUERYING.md).

Page rows plus total count can be two rowsets in one round trip. Validate that the count
rowset contains its required row; a missing count is not silently zero. One batch does not
make two SELECTs observe one snapshot under RCSI. If exact agreement is required, choose an
appropriate transaction-level snapshot or materialize the authorized candidate set once.
Changing isolation requires explicit configuration and cleanup, not an ad hoc `SET` in a query.
