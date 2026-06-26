from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TypeVar, Any
import re
import threading
from dataclasses import dataclass
from enum import Enum

from grad_pylib.core.config import BaseAppSettings, get_settings
from pydantic import BaseModel
from sqlalchemy import URL, create_engine, inspect, Table, Select, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, DeclarativeBase, load_only, sessionmaker
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_incrementing, RetryCallState


def build_mssql_url(odbc_connection_string: str) -> str:
    """
    Build the connection URL for an MSSQL database using an ODBC connection string.

    Arguments:
    odbc_connection_string: str
        The ODBC connection string containing the required connection parameters
        such as driver, server, database, and authentication information.
    """
    return URL.create("mssql+pyodbc", query={"odbc_connect": odbc_connection_string}).render_as_string(
        hide_password=False
    )


def resolve_database_url(settings: BaseAppSettings | None = None) -> str:
    settings = settings or get_settings()
    if settings.database_url:
        return build_mssql_url(settings.database_url)
    raise ValueError("DATABASE_URL must be set.")


class DatabaseRuntime:
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
        session = self.get_session_factory()()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def background_session(self) -> Generator[Session]:
        session = self.get_session_factory()()
        try:
            yield session
        finally:
            session.close()


_default_runtime = DatabaseRuntime(resolve_database_url)


def get_engine() -> Engine:
    """
    Get the SQLAlchemy Engine for the default MSSQL database.

    This function ensures that the engine is created only once and reused across
    multiple calls. It uses a lock to prevent race conditions when multiple threads
    might try to create the engine simultaneously.
    """
    return _default_runtime.get_engine()


class SqlServerErrorType(Enum):
    DEADLOCK = "deadlock"  # Code 1205
    LOCK_TIMEOUT = "lock_timeout"  # Code 1222
    DUPLICATE_KEY = "duplicate_key"  # Codes 2601, 2627
    FOREIGN_KEY_VIOLATION = "foreign_key"  # Code 547
    NOT_NULL_VIOLATION = "not_null"  # Code 515
    RCSI_CONFLICT = "rcsi_conflict"  # Code 3960
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedSqlError:
    error_type: SqlServerErrorType
    native_code: int | None
    driver_message: str
    is_idempotency_hit: bool


def parse_mssql_error(e: DBAPIError, idempotency_markers: tuple[str, ...] = ()) -> ParsedSqlError:
    """
    Parses a pyodbc-based SQLAlchemy exception into a structured data object.
    Safe for any framework, script, or retry loop.
    """
    if not e.orig or not hasattr(e.orig, "args") or len(e.orig.args) < 2:
        return ParsedSqlError(SqlServerErrorType.UNKNOWN, 0, '', False)

    # FIX 1: Extract ONLY the text payload element, not the entire tuple string representation
    driver_message = str(e.orig.args[1])
    sql_state = str(e.orig.args[0])

    # Extract the native trailing token: "(ErrorNumber) (CursorFunction)"
    match = re.search(r"\((\d+)\)\s+\([A-Za-z0-9_]+\)$", driver_message)
    native_code = int(match.group(1)) if match else None

    # Check for transient conditions (Both should be retried!)
    if native_code == 1205:
        return ParsedSqlError(SqlServerErrorType.DEADLOCK, native_code, driver_message, False)

    if native_code == 1222:  # <-- Added lock timeout handling
        return ParsedSqlError(SqlServerErrorType.LOCK_TIMEOUT, native_code, driver_message, False)

    if native_code == 3960:
        return ParsedSqlError(SqlServerErrorType.RCSI_CONFLICT, native_code, driver_message, False)

    if sql_state != '23000':
        return ParsedSqlError(SqlServerErrorType.UNKNOWN, native_code, driver_message, False)

    # 4. Determine structural constraint classification
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

    if error.error_type == SqlServerErrorType.DUPLICATE_KEY:
        # ONLY retry if the duplicate error happened on our specific high-race critical table
        return error.is_idempotency_hit

    # Retrying both isolation locks AND concurrent race states
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


# Reusable decorator for retrying transient SQL Server contention errors
retry_on_transient_conflict = retry(
    retry=retry_if_exception(_is_transient_conflict),
    stop=stop_after_attempt(3),
    wait=wait_incrementing(start=0.05, increment=0.05),
    before_sleep=_rollback_session_before_sleep,
    reraise=True,
)


def _coerce_model_data(
        model_cls: type[DeclarativeBase],
        data_source: dict[str, Any] | BaseModel | DeclarativeBase,
        *,
        all_columns: list[str],
        parameter_name: str,
) -> dict[str, Any]:
    if isinstance(data_source, BaseModel):
        data = data_source.model_dump(exclude_unset=True)
    elif isinstance(data_source, DeclarativeBase):
        data = {key: value for key, value in data_source.__dict__.items() if key in all_columns}
    elif isinstance(data_source, dict):
        data = data_source
    else:
        raise TypeError(f"{parameter_name} must be a dict, Pydantic model, or DeclarativeBase instance")

    return {key: value for key, value in data.items() if key in all_columns}


def orm_upsert[ModelT: DeclarativeBase](
        db: Session,
        model_cls: type[ModelT],
        data_source: dict[str, Any] | BaseModel | DeclarativeBase,
        *,
        insert_only: dict[str, Any] | BaseModel | DeclarativeBase | None = None,
) -> ModelT:
    """
    Universal, concurrency-safe ORM upsert for SQL Server under RCSI.
    Accepts raw dicts, Pydantic models, or sqlacodegen DeclarativeBase instances.
    `insert_only` fields are applied only when creating a new row.
    """
    # Inspect the core database model to find its primary keys
    mapper = inspect(model_cls)
    pk_names = [col.name for col in mapper.primary_key]
    all_columns = [col.name for col in mapper.columns]

    data = _coerce_model_data(
        model_cls,
        data_source,
        all_columns=all_columns,
        parameter_name="data_source",
    )
    insert_only_data = (
        {}
        if insert_only is None
        else _coerce_model_data(
            model_cls,
            insert_only,
            all_columns=all_columns,
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

    # Roundtrip 2: Commit changes securely
    db.flush()
    return record


def select_exclude[BaseT: DeclarativeBase | Table](
        model_or_table: type[BaseT] | Table,
        exclude: set[str]
) -> Select:
    """
    Constructs a select statement excluding specific columns.
    Works seamlessly with both SQLAlchemy ORM Models and Core Tables.
    """
    # 1. Handle Core Table Objects
    if isinstance(model_or_table, Table):
        columns_to_select = [
            col for col in model_or_table.c
            if col.name not in exclude
        ]
        return select(*columns_to_select)

    # 2. Handle ORM Model Classes
    all_columns = model_or_table.__mapper__.column_attrs
    include_attrs = [
        getattr(model_or_table, col.key)
        for col in all_columns
        if col.key not in exclude
    ]

    return (
        select(model_or_table)
        .options(load_only(*include_attrs))
        .execution_options(raise_on_sql_errors=True)
    )
