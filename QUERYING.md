# Querying

`grad_pylib.core.querying` is the shared query-construction layer for both
SQLAlchemy `select(...)` queries and small app-local raw SQL helpers. Prefer
extending or reusing it instead of creating a separate query utility module.

Filtering parameters use a `field` or `field__operator` naming convention:

- `status=submitted`
- `requested_amount__gte=100`
- `department__in=["AA", "BB"]`
- `reviewed_at__isnull=true`
- `reviewed_at__notnull=true`

Supported filter operators are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `like`,
`ilike`, `in`, `isnull`, and `notnull`.

Both query paths share filter normalization: field/operator validation, skipping
`None` values, coercing scalar or collection `in` values to nonempty lists, and
coercing null-check booleans. Core `select(...)` queries and template-string
fragments build SQLAlchemy expressions compiled for the selected dialect (so
`ilike` becomes `lower(x) LIKE lower(y)` on SQL Server). The older string-based
raw SQL helpers emit explicit SQL operators and bound parameters (including
literal `ILIKE`, which SQL Server does not support).

`isnull` and `notnull` expect a boolean value. For example,
`reviewed_at__isnull=true` produces `reviewed_at IS NULL`, while
`reviewed_at__isnull=false` and `reviewed_at__notnull=true` both produce
`reviewed_at IS NOT NULL`.

Sorting uses a comma-separated list of public field names. Prefix a field with
`-` for descending order, for example `sort=-submitted_at,department_code`.

## SQLAlchemy `select(...)` queries

Use `QuerySpec` to declare the public filter and sort names that an endpoint or
service allows, then apply them to a statement with `apply_query()` or the more
focused helpers.

```python
from sqlalchemy import select

from grad_pylib.core.querying import QuerySpec, apply_query

spec = QuerySpec(
    filterable={
        "department_code": Award.department_code,
        "requested_amount": Award.requested_amount,
        "reviewed_at": Award.reviewed_at,
    },
    sortable={
        "department_code": Award.department_code,
        "submitted_at": Award.submitted_at,
    },
    default_sort="-submitted_at",
)

stmt = apply_query(
    select(Award),
    spec,
    filters={
        "department_code": "1227",
        "requested_amount__gte": 100,
        "reviewed_at__notnull": True,
    },
    sort="department_code",
    limit=25,
    offset=0,
)
```

`None` filter values are ignored, so request query parameters can usually be
passed through directly after any application-specific normalization.

## Declaring filters on request models

Use `grad_pylib.core.filtering` to declare which operators each filter field
accepts. Every operator is listed explicitly (`eq` is exposed as the bare field
name), so the request model and OpenAPI spec contain only what the endpoint
supports, with typed values.

```python
from datetime import date
from typing import Annotated

from fastapi import Query

from grad_pylib.core.filtering import FilterField, create_filter_model, filter_values

AwardFilters = create_filter_model(
    "AwardFilters",
    {
        "department_code": FilterField(str, "eq", "in"),
        "submitted_at": FilterField(date, "gte", "lte"),
        "reviewed_at": FilterField(date, "isnull"),
    },
)


class AwardListRequest(AwardFilters):
    sort: str | None = None


@router.get("/awards")
def list_awards(session: DbSession, request: Annotated[AwardListRequest, Query()]):
    stmt = apply_query(
        select(Award),
        spec,
        filters=filter_values(request, AwardFilters),
        sort=request.sort,
    )
```

This declares `department_code`, `department_code__in`, `submitted_at__gte`,
`submitted_at__lte`, and `reviewed_at__isnull`. `in` fields are lists (repeated
query parameters, or a JSON array in a body), `isnull`/`notnull` are booleans, and
`like`/`ilike` are only allowed on `str` fields. `filter_values()` dumps only the
non-`None` filter fields, leaving out other request fields such as `sort`.

Use `escape_like()` when building a `LIKE` pattern from user text that should
match literally, e.g. a "contains" search:

```python
column.like(f"%{escape_like(term)}%", escape="/")
```

## Raw SQL with template strings

For raw SQL, keep the actual SQL visible in a Python 3.14 template string and let
`QuerySpec` own the allowlist. `where_fragment()` and `order_by_fragment()`
return fragments to interpolate into SQLAlchemy's `tstring(...)`:

```python
from sqlalchemy import table, column, tstring

from grad_pylib.core.querying import QuerySpec, order_by_fragment, where_fragment

a = table(
    "awards",
    column("term"),
    column("department"),
    column("degree_program"),
    column("reviewed_at"),
    column("submitted_at"),
).alias("a")

lookup_spec = QuerySpec(
    filterable={
        "department": a.c.department,
        "degree_program": a.c.degree_program,
        "reviewed_at": a.c.reviewed_at,
    },
    sortable={"department": a.c.department, "submitted_at": a.c.submitted_at},
    default_sort="-submitted_at",
)

filters: dict[str, object] = {}
if programs:
    filters["degree_program__in"] = programs
elif departments:
    filters["department__in"] = departments
elif require_reviewed is not None:
    filters["reviewed_at__notnull"] = require_reviewed

where = where_fragment(lookup_spec, filters, tstring(t"a.term = {term}"))
order_by = order_by_fragment(lookup_spec, sort)

query = tstring(
    t"""
    SELECT a.degree_program, a.department
    FROM awards AS a
    {where}
    {order_by}
    """
)
```

- `where_fragment(spec, filters, *conditions)` renders either nothing or a
  complete `WHERE ...` clause. `conditions` are fixed, developer-authored
  predicates, either Core expressions (`a.c.term == term`) or `tstring(...)`
  fragments; they come first and are joined with the filters using `AND`.
  Template fragments are parenthesized so predicates containing `OR` keep their
  grouping.
- `order_by_fragment(spec, sort)` renders either nothing or a complete
  `ORDER BY ...` clause, falling back to `spec.default_sort`.

Values interpolated with `{...}` become bound parameters, and `IN` filters use
expanding parameters automatically, so there are no parameter names to manage.
Statements with the same structure but different values share a statement-cache
entry.

Columns render exactly as SQLAlchemy renders them, including their table or
alias qualifier and dialect quoting. Build the spec from columns that match the
`FROM` clause: `alias.c.x` for an aliased table, the model or table column for an
unaliased one, or an unbound `column("x")` for an unqualified name.

Interpolate values with `{...}`; do not use `:name` parameters inside a template
string. SQLAlchemy 2.1.0 and 2.1.1 still treat `:name` in template text as a bind
parameter, but later releases render it literally.

## Raw SQL with `text(...)`

The string-based helpers remain available for existing `text(...)` queries.

```python
from sqlalchemy import text

from grad_pylib.core.querying import QuerySpec, build_where_clause

lookup_spec = QuerySpec(
    filterable={
        "department": awards.c.department,
        "degree_program": awards.c.degree_program,
        "reviewed_at": awards.c.reviewed_at,
    },
)

filters: dict[str, object] = {}
if programs:
    filters["degree_program__in"] = programs
elif departments:
    filters["department__in"] = departments
elif require_reviewed is not None:
    filters["reviewed_at__notnull"] = require_reviewed

where = build_where_clause(
    lookup_spec,
    filters,
    fixed_clauses=("term = :term",),
)

query = where.bind(
    text(
        f"""
        SELECT degree_program, department
        FROM awards
        {where.sql}
        """
    )
).params(term=term)
```

`build_where_clause()` returns a `RawWhereClause` with:

- `sql`: either `""` or a complete `WHERE ...` clause
- `params`: the dynamically generated bind parameters
- `bind(query)`: applies `params` to a `TextClause` and automatically marks any
  generated `IN` parameters as SQLAlchemy expanding parameters

Use `fixed_clauses=` for developer-authored predicates that should always be
included while preserving the convenience of getting either `""` or a complete
`WHERE ...` clause back. Each predicate is parenthesized before joining with
`AND`, so predicates containing `OR` retain their grouping.

Parameter names beginning with `__grad_pylib_filter_` are reserved for generated
filters; do not use them in fixed predicates or surrounding SQL. `bind(query)`
rejects generated parameters that already have a value (including `None`) or a
callable, before installing expanding parameters. Fixed parameters with distinct
names may be bound before or after calling `bind(query)`.

Keep domain decisions in the application layer, outside `grad_pylib`, such as:

- precedence between two filters (`programs` vs `departments`)
- whether an empty effective scope should short-circuit to `None` or `[]`
- domain-specific fixed predicates and parameter names
