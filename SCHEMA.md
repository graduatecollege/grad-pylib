# Schema helpers

`grad_pylib.core.schemas` includes reusable helpers for common Pydantic
normalization patterns:

* `parse_comma_separated_strings()` parses either a comma-separated string or an existing list,
  strips whitespace, drops blanks, and can optionally deduplicate or sort.
* `parse_validated_comma_separated_strings()` builds on that parser and applies an app-local
  validator to each item.
* `parse_json_blob()` parses JSON when a field arrives as a string and can optionally turn invalid
  JSON into `None`.
* `normalize_email_list()` trims, lowercases, removes blanks, and can require at least one email
  after normalization.

These helpers are intentionally generic. Business rules such as which departments may submit
a request belong in the consuming app.

See [Parameter types](PARAMS.md) for shared validated fields and
[API response conventions](RESPONSES.md) for response models.
