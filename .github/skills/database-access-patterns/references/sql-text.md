# SQL text and parameterization

Use SQL text for a clearer set-based operation, SQL Server-specific features, or a justified
multi-statement round trip. Do not replace straightforward ORM/Core lookups with a batch
merely to use this facility.

## Values and structural SQL

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

## SQL Server types and collection inputs

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
