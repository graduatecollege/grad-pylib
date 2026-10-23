from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Annotated, Any, Self, TypeGuard
import functools
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from inspect import iscoroutinefunction

import structlog
from fastapi import Depends
from grad_pylib.core.config import BaseAppSettings, get_settings
from pydantic import BaseModel
from sqlalchemy import URL, create_engine, event, inspect, Table, Select, select
from sqlalchemy.engine import Connection, Engine, ExceptionContext
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.orm import Session, DeclarativeBase, load_only, sessionmaker
from sqlalchemy.pool import PoolResetState
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_incrementing, RetryCallState

_logger = structlog.get_logger(__name__)


def build_mssql_url(odbc_connection_string: str) -> str:
    """Convert an ODBC connection string to the SQLAlchemy URL used by this library.

    The password is intentionally retained because SQLAlchemy passes the complete string to
    `pyodbc`; avoid logging the returned URL or the source connection string.
    """
    return URL.create("mssql+pyodbc", query={"odbc_connect": odbc_connection_string}).render_as_string(
        hide_password=False
    )


def resolve_database_url(settings: BaseAppSettings | None = None) -> str:
    """Build the default database URL from settings or raise when it is not configured.

    Passing settings supports scripts and tests; omitted settings come from the shared cached
    application configuration used by the default single-database setup.
    """
    settings = settings or get_settings()
    if settings.database_url:
        return build_mssql_url(settings.database_url)
    raise ValueError("DATABASE_URL must be set.")


# Firewalls and NAT between the cluster and SQL Server silently drop idle TCP connections. A
# pre-ping on such a connection blocks until the OS TCP timeout and can surface as ODBC 08S01
# (TCP error 10060), so connections are replaced before checkout once they reach this age.
DEFAULT_POOL_RECYCLE_SECONDS = 300

# pyodbc ends each driver message with the failing ODBC function; SQLEndTran is the commit call.
_COMMIT_DRIVER_FUNCTION = "(SQLEndTran"
_TERMINATED_SESSION_MESSAGE = "Connection may have been terminated by the server"
_CONNECTED_AT_INFO_KEY = "grad_pylib_connected_at"
_NEEDS_RESET_INFO_KEY = "grad_pylib_needs_reset"


def is_disconnect_error(exc: BaseException) -> TypeGuard[DBAPIError]:
    """Return whether SQLAlchemy discarded the connection because it was lost or unusable.

    The pool replaces such connections automatically, so only the operation in flight failed.
    """
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def is_retryable_disconnect(exc: BaseException) -> TypeGuard[DBAPIError]:
    """Return whether a lost connection certainly left no committed work behind.

    SQL Server rolls back an open transaction when its connection drops, so losing the connection
    while connecting or executing a statement is safe to retry. Losing it during commit is not: the
    commit may already have been applied. Detection relies on pyodbc naming the failing ODBC
    function in its message, which holds for the `mssql+pyodbc` dialect this library targets.
    """
    return is_disconnect_error(exc) and _COMMIT_DRIVER_FUNCTION not in _driver_message(exc.orig)


def describe_disconnect(exc: DBAPIError) -> dict[str, Any]:
    """Return log fields describing a lost connection without the SQL statement or parameters."""
    return _driver_error_fields(exc.orig)


def _driver_message(driver_error: BaseException | None) -> str:
    args = getattr(driver_error, "args", ())
    return str(args[1]) if len(args) > 1 else ""


def _driver_error_fields(driver_error: BaseException | None) -> dict[str, Any]:
    args = getattr(driver_error, "args", ())
    message = _driver_message(driver_error)
    return {
        "sqlstate": str(args[0]) if args else None,
        "driver_message": message,
        "during_commit": _COMMIT_DRIVER_FUNCTION in message,
    }


def _record_connect_time(_dbapi_connection: Any, connection_record: Any) -> None:
    connection_record.info[_CONNECTED_AT_INFO_KEY] = time.monotonic()


def _log_disconnect(context: ExceptionContext) -> None:
    # An exception escaping a handle_error listener would replace the database error being raised.
    try:
        if not context.is_disconnect:
            return

        fields = _driver_error_fields(context.original_exception)
        if context.is_pre_ping:
            _logger.info("db.disconnect.pre_ping", **fields)
            return

        connection = context.connection
        # Reading `info` from an invalidated Connection would make SQLAlchemy reconnect it.
        has_live_connection = connection is not None and not connection.invalidated and not connection.closed
        connected_at = connection.info.get(_CONNECTED_AT_INFO_KEY) if has_live_connection else None
        connection_age = round(time.monotonic() - connected_at, 3) if connected_at is not None else None
        _logger.warning("db.disconnect", connection_age_seconds=connection_age, **fields)
    except Exception:
        pass


def _detect_terminated_session(context: ExceptionContext) -> None:
    # pyodbc reports a session the server ended (KILL, severity 20+ errors) as a general HY000 error that
    # the dialect does not classify. Other HY000 errors, such as a busy connection, leave it usable.
    args = getattr(context.original_exception, "args", ())
    if args and args[0] == "HY000" and _TERMINATED_SESSION_MESSAGE in _driver_message(context.original_exception):
        context.is_disconnect = True


def install_terminated_session_detection(engine: Engine) -> None:
    """Treat sessions SQL Server terminated mid-request as lost connections.

    pyodbc reports these with the catch-all SQLSTATE HY000, so SQLAlchemy would return the dead
    connection to the pool and `retry_on_disconnect` would not retry. The server has rolled back
    the session's work, so the usual disconnect handling and retry rules apply.
    """
    if engine.dialect.name != "mssql":
        return
    # Runs before other listeners so they see the corrected classification.
    event.listen(engine, "handle_error", _detect_terminated_session, insert=True)


def _flag_for_reset(context: ExceptionContext) -> None:
    # An exception escaping a handle_error listener would replace the database error being raised.
    try:
        connection = context.connection
        # Lost connections are discarded anyway, and reading `info` from one would reconnect it.
        if (connection is not None and not context.is_disconnect
                and not connection.invalidated and not connection.closed):
            connection.info[_NEEDS_RESET_INFO_KEY] = True
    except Exception:
        pass


def _reset_after_error(dbapi_connection: Any, connection_record: Any, reset_state: PoolResetState) -> None:
    needs_reset = connection_record is not None and connection_record.info.pop(_NEEDS_RESET_INFO_KEY, False)
    if needs_reset and not reset_state.terminate_only:
        dbapi_connection.execute("{call sys.sp_reset_connection}")
        # Keeps pyodbc's transaction tracking consistent after the call above.
        dbapi_connection.rollback()


def install_connection_reset(engine: Engine) -> None:
    """Reset SQL Server session state when a connection that raised a database error returns to the pool.

    A batch that fails partway can skip its own cleanup, such as `SET NOCOUNT OFF`, leaving session
    state for the next checkout. Only those connections pay for the `sp_reset_connection` round trip;
    clean returns keep SQLAlchemy's default rollback-on-return.
    """
    if engine.dialect.name != "mssql":
        return
    event.listen(engine, "handle_error", _flag_for_reset)
    event.listen(engine, "reset", _reset_after_error)


@contextmanager
def _sqlalchemy_cursor_errors(
        connection: Connection,
        cursor: DBAPICursor | None,
        *,
        context: ExecutionContext | None = None,
        statement: str | None = None,
) -> Generator[None]:
    """Handle failures of direct DB-API cursor operations as SQLAlchemy handles its own.

    SQLAlchemy only adapts errors from cursor work it drives itself and has no public API for
    work it does not, such as `nextset()` or a raw script cursor. This calls the private
    `Connection._handle_dbapi_exception` that SQLAlchemy's own result fetching uses, so a driver
    failure becomes the matching `DBAPIError` subclass chained from the driver error kept in
    `.orig`, runs `handle_error` listeners (disconnect logging, reset marking), and invalidates
    lost connections without reconnecting them. Keep every use of that private API here; its
    compatibility tests must be rerun on SQLAlchemy upgrades.

    Pass the execution context for a result's cursor, or the statement for a raw cursor so it
    appears in the error. Errors SQLAlchemy already handled are re-raised untouched, and other
    exceptions keep their identity. Like SQLAlchemy, this closes the cursor after errors other
    than disconnects; commit and rollback of the transaction remain with its owner.
    """
    try:
        yield
    except StatementError:
        raise
    except BaseException as error:
        is_driver_error = isinstance(error, connection.dialect.loaded_dbapi.Error)
        # A statement without a context would also make SQLAlchemy wrap non-driver exceptions.
        connection._handle_dbapi_exception(error, statement if is_driver_error else None, None, cursor, context)


def _install_disconnect_logging(engine: Engine) -> None:
    """Log each lost connection with its age so drops can be correlated with network events.

    Pre-ping failures are logged at info level because the pool recovers from them transparently.
    """
    event.listen(engine, "connect", _record_connect_time)
    event.listen(engine, "handle_error", _log_disconnect)


class DatabaseRuntime:
    """Lazily create and share one SQLAlchemy engine and session factory for a database.

    The URL resolver is invoked only when the engine is first needed, allowing application
    settings to be registered before startup. Thread-safe initialization makes the runtime safe
    to expose as an application-level singleton; sessions remain request or task scoped.
    `pool_recycle` replaces pooled connections older than the given number of seconds without
    contacting the server, so long-idle connections are never pinged or reused; pass -1 to disable.
    """
    def __init__(
            self,
            database_url_resolver: Callable[[], str],
            *,
            pool_pre_ping: bool = True,
            pool_size: int = 5,
            max_overflow: int = 20,
            pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS,
    ) -> None:
        self._database_url_resolver = database_url_resolver
        self._pool_pre_ping = pool_pre_ping
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_recycle = pool_recycle
        self._engine: Engine | None = None
        self._engine_lock = threading.Lock()
        self._session_factory: sessionmaker[Session] | None = None
        self._session_factory_lock = threading.Lock()

    def get_engine(self) -> Engine:
        """Return the lazily created, pooled engine for this runtime."""
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    engine = create_engine(
                        self._database_url_resolver(),
                        pool_pre_ping=self._pool_pre_ping,
                        pool_size=self._pool_size,
                        max_overflow=self._max_overflow,
                        pool_recycle=self._pool_recycle,
                    )
                    install_terminated_session_detection(engine)
                    install_connection_reset(engine)
                    _install_disconnect_logging(engine)
                    self._engine = engine
                    return engine
        return self._engine

    def get_session_factory(self) -> sessionmaker[Session]:
        """Return the cached factory configured for explicit flush and transaction control.

        Instances do not autoflush or expire committed objects, leaving transaction boundaries to
        the caller while keeping returned ORM objects usable after a commit.
        """
        if self._session_factory is None:
            with self._session_factory_lock:
                if self._session_factory is None:
                    factory = sessionmaker(
                        bind=self.get_engine(),
                        autoflush=False,
                        autocommit=False,
                        expire_on_commit=False,
                    )
                    self._session_factory = factory
                    return factory
        return self._session_factory

    def session(self) -> Generator[Session]:
        """Yield a FastAPI-compatible session and close it after the request dependency ends.

        This manages resource cleanup only; handlers remain responsible for commit and rollback.
        """
        session = self.get_session_factory()()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def background_session(self) -> Generator[Session]:
        """Provide a context-managed session for scripts and background work.

        Use `with runtime.background_session()` outside FastAPI; as with `session`, callers own
        transaction handling and the context manager guarantees closing the session.
        """
        session = self.get_session_factory()()
        try:
            yield session
        finally:
            session.close()


def _settings_database_url_resolver(
        settings_provider: Callable[[], Any],
        field_name: str,
        *,
        url_builder: Callable[[str], str],
) -> Callable[[], str]:
    def resolve() -> str:
        settings = settings_provider()
        database_url = getattr(settings, field_name, None)
        if database_url:
            return url_builder(database_url)
        raise ValueError(f"{field_name.upper()} must be set.")

    return resolve


@dataclass(frozen=True, slots=True)
class NamedDatabase:
    """Bound engine, session, and dependency helpers for one database in `NamedDatabases`.

    Keep a named helper when a route or task always uses the same non-default database; it avoids
    repeating the database name while retaining the owning application's explicit bootstrap.
    """

    name: str
    runtime: DatabaseRuntime

    def get_engine(self) -> Engine:
        """Return this named database's shared engine."""
        return self.runtime.get_engine()

    def get_session(self) -> Generator[Session]:
        """Yield this named database's request-scoped session."""
        yield from self.runtime.session()

    def get_background_session(self) -> AbstractContextManager[Session]:
        """Return a context manager for a session used outside request dependencies."""
        return self.runtime.background_session()

    def session_dependency(self) -> Any:
        """Return the typed FastAPI dependency annotation for this database's session."""
        return Annotated[Session, Depends(self.get_session)]


class NamedDatabases:
    """
    Shared multi-database bootstrap for FastAPI applications.

    Example:
        databases = NamedDatabases.from_settings(
            get_settings,
            {"app": "database_url", "codebook": "codebook_database_url"},
            default_name="app",
        )

        app_db = databases.default
        codebook_db = databases["codebook"]

        get_engine = databases.get_engine
        get_codebook_engine = codebook_db.get_engine

        get_session = databases.get_session
        get_codebook_session = codebook_db.get_session

        DbSession = databases.session_dependency()
        CodebookDbSession = codebook_db.session_dependency()

        get_background_session = databases.get_background_session
        get_codebook_background_session = codebook_db.get_background_session
    """

    def __init__(
            self,
            database_url_resolvers: Mapping[str, Callable[[], str]],
            *,
            default_name: str | None = None,
            pool_pre_ping: bool = True,
            pool_size: int = 5,
            max_overflow: int = 20,
            pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS,
    ) -> None:
        if not database_url_resolvers:
            raise ValueError("At least one named database must be configured.")

        self._databases: dict[str, NamedDatabase] = {}

        for name, resolver in database_url_resolvers.items():
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Database names must not be blank.")
            if normalized_name in self._databases:
                raise ValueError(f"Duplicate database name: {normalized_name}")

            runtime = DatabaseRuntime(
                resolver,
                pool_pre_ping=pool_pre_ping,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_recycle=pool_recycle,
            )
            self._databases[normalized_name] = NamedDatabase(normalized_name, runtime)

        self._default_name: str | None = None
        if default_name is not None:
            normalized_default_name = default_name.strip()
            if not normalized_default_name:
                raise ValueError("Default database name must not be blank.")
            if normalized_default_name not in self._databases:
                raise KeyError(f"Unknown default database: {default_name!r}")
            self._default_name = normalized_default_name

    @classmethod
    def from_settings(
            cls,
            settings_provider: Callable[[], Any],
            database_fields: Mapping[str, str],
            *,
            default_name: str | None = None,
            url_builder: Callable[[str], str] = build_mssql_url,
            pool_pre_ping: bool = True,
            pool_size: int = 5,
            max_overflow: int = 20,
            pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS,
    ) -> Self:
        """Build named runtimes whose URLs are read lazily from application settings.

        `database_fields` maps public database names to settings attributes. The default MSSQL
        URL builder can be replaced for tests or databases using another SQLAlchemy dialect.
        """
        return cls(
            {
                name: _settings_database_url_resolver(settings_provider, field_name, url_builder=url_builder)
                for name, field_name in database_fields.items()
            },
            default_name=default_name,
            pool_pre_ping=pool_pre_ping,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_recycle=pool_recycle,
        )

    @property
    def names(self) -> tuple[str, ...]:
        """Return configured database names in their registration order."""
        return tuple(self._databases)

    @property
    def default_name(self) -> str | None:
        """Return the configured default name, if convenience helpers may use one."""
        return self._default_name

    @property
    def default(self) -> NamedDatabase:
        """Return the default database or raise if none was configured."""
        if self._default_name is None:
            raise ValueError("No default database configured.")
        return self._databases[self._default_name]

    def __getitem__(self, name: str) -> NamedDatabase:
        """Return the named database, supporting concise application bootstrap access."""
        return self.database(name)

    def _database_or_default(self, name: str | None) -> NamedDatabase:
        if name is None:
            return self.default
        return self.database(name)

    def database(self, name: str) -> NamedDatabase:
        """Return a configured database by normalized name or raise a descriptive `KeyError`."""
        try:
            return self._databases[name.strip()]
        except KeyError as exc:
            raise KeyError(f"Unknown database: {name!r}") from exc

    def get_runtime(self) -> DatabaseRuntime:
        """Return the default runtime without accepting request-bound parameters."""
        return self.default.runtime

    def get_engine(self) -> Engine:
        """Return the default engine without accepting request-bound parameters."""
        return self.default.get_engine()

    def get_session(self) -> Generator[Session]:
        """Yield a default database session without accepting request-bound parameters."""
        yield from self.default.get_session()

    def get_background_session(self) -> AbstractContextManager[Session]:
        """Return a default database session context manager with no request-bound parameters."""
        return self.default.get_background_session()

    def session_dependency(self, name: str | None = None) -> Any:
        """Bind a database at setup time and return its parameterless FastAPI session annotation."""
        return self._database_or_default(name).session_dependency()


class SqlServerErrorType(Enum):
    """SQL Server conditions recognized by `parse_mssql_error` for application handling."""
    DEADLOCK = "deadlock"  # Code 1205
    LOCK_TIMEOUT = "lock_timeout"  # Code 1222
    DUPLICATE_KEY = "duplicate_key"  # Codes 2601, 2627
    FOREIGN_KEY_VIOLATION = "foreign_key"  # Code 547
    CHECK_CONSTRAINT_VIOLATION = "check_constraint"  # Code 547
    NOT_NULL_VIOLATION = "not_null"  # Code 515
    DATA_TRUNCATION = "data_truncation"  # Codes 2628, 8152
    DATA_CONVERSION = "data_conversion"  # Codes 241, 245, 8114
    ARITHMETIC_OVERFLOW = "arithmetic_overflow"  # Codes 220, 8115
    DIVIDE_BY_ZERO = "divide_by_zero"  # Code 8134
    RCSI_CONFLICT = "rcsi_conflict"  # Code 3960
    INVALID_COLUMN = "invalid_column"  # Code 207
    INVALID_OBJECT = "invalid_object"  # Code 208
    PERMISSION_DENIED = "permission_denied"  # Code 229
    LOGIN_FAILED = "login_failed"  # Code 18456
    UNKNOWN = "unknown"


_BAD_DATA_ERROR_TYPES = frozenset({
    SqlServerErrorType.DUPLICATE_KEY,
    SqlServerErrorType.FOREIGN_KEY_VIOLATION,
    SqlServerErrorType.CHECK_CONSTRAINT_VIOLATION,
    SqlServerErrorType.NOT_NULL_VIOLATION,
    SqlServerErrorType.DATA_TRUNCATION,
    SqlServerErrorType.DATA_CONVERSION,
    SqlServerErrorType.ARITHMETIC_OVERFLOW,
})


@dataclass(frozen=True, slots=True)
class ParsedSqlError:
    """Structured SQL Server driver failure used to make retry and idempotency decisions.

    `is_idempotency_hit` is true only for duplicate-key messages matching caller-provided markers;
    it distinguishes an expected concurrent insert from an unrelated uniqueness failure.
    """
    error_type: SqlServerErrorType
    native_code: int | None
    driver_message: str
    is_idempotency_hit: bool

    @property
    def is_data_validation_error(self) -> bool:
        """Return whether SQL Server rejected invalid or unpersistable data."""
        return self.error_type in _BAD_DATA_ERROR_TYPES


def parse_mssql_error(e: DBAPIError, idempotency_markers: tuple[str, ...] = ()) -> ParsedSqlError:
    """Classify a pyodbc SQLAlchemy error by SQL Server native code.

    This defensive parser is safe for request handlers, scripts, and retry loops. Duplicate-key
    errors count as idempotent only when their driver message contains a supplied marker.
    The first native diagnostic code takes precedence over subsequent informational diagnostics;
    classification uses native codes because ODBC SQL states vary by driver and error category.
    """
    if not e.orig or not hasattr(e.orig, "args") or len(e.orig.args) < 2:
        return ParsedSqlError(SqlServerErrorType.UNKNOWN, 0, '', False)

    driver_message = str(e.orig.args[1])

    # ODBC may append more diagnostics after the primary error, such as SQL Server code 3621.
    match = re.search(
        r"\((\d+)\)(?:\s+\(SQL[A-Za-z0-9_]+\))?\s*(?=;\s*\[[A-Z0-9]{5}]|\Z)",
        driver_message,
    )
    native_code = int(match.group(1)) if match else None

    if native_code == 1205:
        return ParsedSqlError(SqlServerErrorType.DEADLOCK, native_code, driver_message, False)

    if native_code == 1222:
        return ParsedSqlError(SqlServerErrorType.LOCK_TIMEOUT, native_code, driver_message, False)

    if native_code == 3960:
        return ParsedSqlError(SqlServerErrorType.RCSI_CONFLICT, native_code, driver_message, False)

    if native_code in (2601, 2627):
        is_idempotent = any(marker in driver_message for marker in idempotency_markers)
        return ParsedSqlError(SqlServerErrorType.DUPLICATE_KEY, native_code, driver_message, is_idempotent)

    if native_code == 547:
        if "CHECK constraint" in driver_message:
            return ParsedSqlError(SqlServerErrorType.CHECK_CONSTRAINT_VIOLATION, native_code, driver_message, False)
        return ParsedSqlError(SqlServerErrorType.FOREIGN_KEY_VIOLATION, native_code, driver_message, False)

    if native_code == 515:
        return ParsedSqlError(SqlServerErrorType.NOT_NULL_VIOLATION, native_code, driver_message, False)

    if native_code in (2628, 8152):
        return ParsedSqlError(SqlServerErrorType.DATA_TRUNCATION, native_code, driver_message, False)

    if native_code in (241, 245, 8114):
        return ParsedSqlError(SqlServerErrorType.DATA_CONVERSION, native_code, driver_message, False)

    if native_code in (220, 8115):
        return ParsedSqlError(SqlServerErrorType.ARITHMETIC_OVERFLOW, native_code, driver_message, False)

    if native_code == 8134:
        return ParsedSqlError(SqlServerErrorType.DIVIDE_BY_ZERO, native_code, driver_message, False)

    if native_code == 207:
        return ParsedSqlError(SqlServerErrorType.INVALID_COLUMN, native_code, driver_message, False)

    if native_code == 208:
        return ParsedSqlError(SqlServerErrorType.INVALID_OBJECT, native_code, driver_message, False)

    if native_code == 229:
        return ParsedSqlError(SqlServerErrorType.PERMISSION_DENIED, native_code, driver_message, False)

    if native_code == 18456:
        return ParsedSqlError(SqlServerErrorType.LOGIN_FAILED, native_code, driver_message, False)

    return ParsedSqlError(SqlServerErrorType.UNKNOWN, native_code, driver_message, False)


def _is_transient_conflict(exc: BaseException) -> bool:
    """Return True for transient SQL Server contention or a lost connection that is safe to retry."""
    if not isinstance(exc, DBAPIError):
        return False

    if is_retryable_disconnect(exc):
        return True

    error = parse_mssql_error(exc)

    return error.error_type in {
        SqlServerErrorType.DEADLOCK,
        SqlServerErrorType.LOCK_TIMEOUT,
        SqlServerErrorType.RCSI_CONFLICT
    }


def _log_retry(operation: Callable[..., Any] | None, attempt: int, exc: DBAPIError, next_wait: float) -> None:
    if is_disconnect_error(exc):
        reason_fields = {"reason": "connection_lost", **describe_disconnect(exc)}
    else:
        parsed = parse_mssql_error(exc)
        reason_fields = {"reason": parsed.error_type.value, "native_code": parsed.native_code}
    _logger.warning(
        "db.retry",
        operation=getattr(operation, "__qualname__", None),
        attempt=attempt,
        next_wait_seconds=next_wait,
        **reason_fields,
    )


def _rollback_sessions(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
    """Roll back every `Session` passed to a failed call so the retry can use it again."""
    for value in (*args, *kwargs.values()):
        if isinstance(value, Session):
            value.rollback()


def _log_before_retry_sleep(operation: Callable[..., Any], retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, DBAPIError):
        _log_retry(operation, retry_state.attempt_number, exc, retry_state.upcoming_sleep)


def _transaction_session(func: Callable[..., Any], args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Session:
    name = getattr(func, "__qualname__", repr(func))
    sessions = [value for value in (*args, *kwargs.values()) if isinstance(value, Session)]
    if len(sessions) != 1:
        raise TypeError(
            f"{name} must receive exactly one Session argument to run in a retryable "
            f"transaction; got {len(sessions)}."
        )
    session = sessions[0]
    if session.in_transaction():
        raise RuntimeError(
            f"{name} must own its transaction, but its session already has one in progress. "
            "Commit or roll back earlier work before calling it, and move reads the write depends on "
            "into the decorated function."
        )
    return session


def retry_on_transient_conflict[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Run a database write in its own transaction, retrying it after transient SQL Server failures.

    The decorated function receives exactly one `Session` argument; the decorator begins a
    transaction on it, commits when the function returns, and rolls back if it raises. The
    function must not commit. Each attempt is a complete transaction, so a retry reruns all of
    the function's reads and writes against current database state.

    Deadlocks, lock timeouts, and RCSI conflicts with concurrent transactions are retried, as is a
    connection lost while connecting or executing a statement. SQL Server aborts the whole
    transaction on a deadlock or lost connection, so retrying is only correct when the decorated
    function owns all of it. A connection lost during commit is re-raised because the commit may
    have been applied (see `is_retryable_disconnect`).

    Calls make up to three attempts, waiting 50 then 100 milliseconds, log each retry as
    `db.retry`, and re-raise the final error. A `TypeError` is raised unless exactly one `Session`
    is passed, and a `RuntimeError` is raised if that session already has a transaction in
    progress, including one autobegun by an earlier read or by an enclosing decorated call.
    """
    if iscoroutinefunction(func):
        raise TypeError("retry_on_transient_conflict supports synchronous functions only.")

    @retry(
        retry=retry_if_exception(_is_transient_conflict),
        stop=stop_after_attempt(3),
        wait=wait_incrementing(start=0.05, increment=0.05),
        before_sleep=functools.partial(_log_before_retry_sleep, func),
        reraise=True,
    )
    def attempt(session: Session, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with session.begin():
            return func(*args, **kwargs)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        session = _transaction_session(func, args, kwargs)
        return attempt(session, *args, **kwargs)

    return wrapper


def retry_on_disconnect[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Run a read-only function once more if its database connection is lost.

    Intended for read endpoints, applied below the route decorator:

        @router.get("/items")
        @retry_on_disconnect
        def read_items(session: DbSession) -> ...:

    The pool replaces a lost connection, so the second attempt runs on a fresh one. Every `Session`
    argument is rolled back first so it can be used again. Failures inside FastAPI dependencies
    happen before the function runs and are not retried. Connections lost during commit are not
    retried either (see `is_retryable_disconnect`), and neither is a second failure. Logs
    `db.retry` before retrying and `db.retry.succeeded` when the retry works. Never apply to
    functions that write; use `retry_on_transient_conflict` for those.
    """
    if iscoroutinefunction(func):
        raise TypeError("retry_on_disconnect supports synchronous functions only.")

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except DBAPIError as exc:
            if not is_retryable_disconnect(exc):
                raise
            _log_retry(func, 1, exc, 0.0)
            _rollback_sessions(args, kwargs)

        result = func(*args, **kwargs)
        _logger.info("db.retry.succeeded", operation=getattr(func, "__qualname__", None))
        return result

    return wrapper


def _coerce_model_data(
        data_source: dict[str, Any] | BaseModel | DeclarativeBase,
        *,
        attribute_names: set[str],
        parameter_name: str,
) -> dict[str, Any]:
    if isinstance(data_source, BaseModel):
        data = data_source.model_dump(exclude_unset=True)
    elif isinstance(data_source, DeclarativeBase):
        data = data_source.__dict__
    elif isinstance(data_source, dict):
        data = data_source
    else:
        raise TypeError(f"{parameter_name} must be a dict, Pydantic model, or DeclarativeBase instance")

    return {key: value for key, value in data.items() if key in attribute_names}


def orm_upsert[ModelT: DeclarativeBase](
        db: Session,
        model_cls: type[ModelT],
        data_source: dict[str, Any] | BaseModel | DeclarativeBase,
        *,
        insert_only: dict[str, Any] | BaseModel | DeclarativeBase | None = None,
) -> ModelT:
    """Insert or update a SQL Server ORM row by its complete primary key under RCSI.

    The lookup uses update and hold locks to serialize competing upserts. Payloads may be dicts,
    Pydantic models, or generated ORM objects; `insert_only` values are applied exclusively to
    new rows and never overwrite an existing record. This flushes but does not commit.
    Payload keys must use mapped ORM attribute names, not physical database column names.
    """
    mapper = inspect(model_cls)
    pk_names = [mapper.get_property_by_column(col).key for col in mapper.primary_key]
    attribute_names = {prop.key for prop in mapper.column_attrs}

    data = _coerce_model_data(
        data_source,
        attribute_names=attribute_names,
        parameter_name="data_source",
    )
    insert_only_data = (
        {}
        if insert_only is None
        else _coerce_model_data(
            insert_only,
            attribute_names=attribute_names,
            parameter_name="insert_only",
        )
    )
    duplicate_keys = data.keys() & insert_only_data.keys()
    if duplicate_keys:
        columns = ", ".join(sorted(duplicate_keys))
        raise ValueError(f"Duplicate keys provided in data_source and insert_only for {model_cls.__name__}: {columns}")

    # Build the strict lookup filter criteria
    filter_criteria = {pk: data[pk] for pk in pk_names if pk in data}
    if len(filter_criteria) != len(pk_names):
        raise ValueError(f"Provided data missing primary key values for {model_cls.__name__}")

    # Roundtrip 1: Query with hints to secure the lock and bypass snapshots
    record = (
        db.query(model_cls)
        .with_hint(model_cls, "WITH (UPDLOCK, HOLDLOCK)")
        .filter_by(**filter_criteria)
        .first()
    )

    if record:
        # Update path: Map fields onto the existing tracked instance
        for key, value in data.items():
            if key not in pk_names:
                setattr(record, key, value)
    else:
        # Insert path: Pass the cleaned dictionary straight into the model constructor
        record = model_cls(**data, **insert_only_data)
        db.add(record)

    db.flush()
    return record


def select_exclude[BaseT: DeclarativeBase | Table](
        model_or_table: type[BaseT] | Table,
        exclude: set[str]
) -> Select:
    """Build a select that omits named columns from a Core table or ORM model.

    Core statements select only the retained columns. ORM statements return model instances with
    excluded mapped columns deferred, preventing their data from being loaded until accessed.
    Raises `ValueError` if no columns remain. ORM deferral is not a data-redaction boundary.
    """
    # 1. Handle Core Table Objects
    if isinstance(model_or_table, Table):
        columns_to_select = [
            col for col in model_or_table.c
            if col.name not in exclude
        ]
        if not columns_to_select:
            raise ValueError("select_exclude must retain at least one column.")
        return select(*columns_to_select)

    # 2. Handle ORM Model Classes
    all_columns = model_or_table.__mapper__.column_attrs
    include_attrs = [
        getattr(model_or_table, col.key)
        for col in all_columns
        if col.key not in exclude
    ]
    if not include_attrs:
        raise ValueError("select_exclude must retain at least one column.")

    return select(model_or_table).options(load_only(*include_attrs))
