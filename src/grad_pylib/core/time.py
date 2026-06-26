from datetime import UTC, datetime


def utc_now() -> datetime:
    """Naive UTC 'now' to match the DATETIME2 columns (which store no tzinfo)."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_from_millis(millis: int) -> datetime:
    """Naive UTC from epoch milliseconds (client idempotency timestamps)."""
    return datetime.fromtimestamp(millis / 1000, UTC).replace(tzinfo=None)
