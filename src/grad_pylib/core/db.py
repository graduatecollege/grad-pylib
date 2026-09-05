from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Annotated, Any, Self
import re
import threading
from dataclasses import dataclass
from enum import Enum

from fastapi import Depends
from grad_pylib.core.config import BaseAppSettings, get_settings
from pydantic import BaseModel
from sqlalchemy import URL, create_engine, inspect, Table, Select, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, DeclarativeBase, load_only, sessionmaker
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_incrementing, RetryCallState


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


class DatabaseRuntime:
    """Lazily create and share one SQLAlchemy engine and session factory for a database.

    The URL resolver is invoked only when the engine is first needed, allowing application
    settings to be registered before startup. Thread-safe initialization makes the runtime safe
    to expose as an application-level singleton; sessions remain request or task scoped.
    """
    def __init__(
            self,
            database_url_resolver: Callable[[], str],
            *,
            pool_pre_ping: bool = True,
            pool_size: int = 5,
            max_overflow: int = 20,
    ) -> None:
        self._database_url_resolver = database_url_resolver
        self._pool_pre_ping = pool_pre_ping
        self._pool_size = pool_size
        self._max_overflow = max_overflow
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
                    )
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
    NOT_NULL_VIOLATION = "not_null"  # Code 515
    RCSI_CONFLICT = "rcsi_conflict"  # Code 3960
    UNKNOWN = "unknown"


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


def parse_mssql_error(e: DBAPIError, idempotency_markers: tuple[str, ...] = ()) -> ParsedSqlError:
    """Classify a pyodbc SQLAlchemy error by SQL Server native code.

    This defensive parser is safe for request handlers, scripts, and retry loops. Duplicate-key
    errors count as idempotent only when their driver message contains a supplied marker.
    The first native diagnostic code takes precedence over subsequent informational diagnostics.
    """
    if not e.orig or not hasattr(e.orig, "args") or len(e.orig.args) < 2:
        return ParsedSqlError(SqlServerErrorType.UNKNOWN, 0, '', False)

    driver_message = str(e.orig.args[1])
    sql_state = str(e.orig.args[0])

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

    if sql_state != '23000':
        return ParsedSqlError(SqlServerErrorType.UNKNOWN, native_code, driver_message, False)

    if native_code in (2601, 2627):
        is_idempotent = any(marker in driver_message for marker in idempotency_markers)
        return ParsedSqlError(SqlServerErrorType.DUPLICATE_KEY, native_code, driver_message, is_idempotent)

    elif native_code == 547:
        return ParsedSqlError(SqlServerErrorType.FOREIGN_KEY_VIOLATION, native_code, driver_message, False)

    elif native_code == 515:
        return ParsedSqlError(SqlServerErrorType.NOT_NULL_VIOLATION, native_code, driver_message, False)

    return ParsedSqlError(SqlServerErrorType.UNKNOWN, native_code, driver_message, False)


def _is_transient_conflict(exc: BaseException) -> bool:
    """Return True for transient SQL Server modifications or race conditions."""
    if not isinstance(exc, DBAPIError):
        return False

    error = parse_mssql_error(exc)

    return error.error_type in {
        SqlServerErrorType.DEADLOCK,
        SqlServerErrorType.LOCK_TIMEOUT,
        SqlServerErrorType.RCSI_CONFLICT
    }


def _rollback_session_before_sleep(retry_state: RetryCallState):
    """Automatically rolls back the DB session associated with the failed call."""
    # Look for the 'db' or 'session' argument passed to the function
    kwargs = retry_state.kwargs
    args = retry_state.args

    session = next((arg for arg in args if isinstance(arg, Session)), None)
    if not session:
        session = next((val for val in kwargs.values() if isinstance(val, Session)), None)

    if session:
        session.rollback()


def retry_on_transient_conflict[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Retry an idempotent database operation after transient SQL Server contention.

    Deadlocks, lock timeouts, and RCSI conflicts on concurrent transactions can prevent this
    attempt from safely completing when the request itself may be valid. A short retry lets
    the competing transaction finish and reruns the operation against current database state.
    Decorated calls make up to three attempts, waiting 50 then 100 milliseconds. Before each retry,
    a passed `Session` is rolled back so it can be used again; the final database error is
    re-raised. Use only for operations that are safe to repeat, and pass the session as an
    argument so failed transactions are reset.
    """
    return retry(
        retry=retry_if_exception(_is_transient_conflict),
        stop=stop_after_attempt(3),
        wait=wait_incrementing(start=0.05, increment=0.05),
        before_sleep=_rollback_session_before_sleep,
        reraise=True,
    )(func)


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
