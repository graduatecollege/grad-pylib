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

## Raw SQL helpers

For raw SQL, keep the actual SQL visible and let `QuerySpec` own the generic
allowlist and parameter-building mechanics.

```python
from sqlalchemy import text

from grad_pylib.core.querying import QuerySpec, build_where_clause

lookup_spec = QuerySpec(
    filterable={
        "department": awards.c.department,
        "degree_program": awards.c.degree_program,
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
        FROM degree_program_lookup
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
`WHERE ...` clause back.

Keep domain decisions in the application layer, outside `grad_pylib`, such as:

- precedence between two filters (`programs` vs `departments`)
- whether an empty effective scope should short-circuit to `None` or `[]`
- domain-specific fixed predicates and parameter names
