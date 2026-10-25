from collections.abc import Callable, Generator
from dataclasses import dataclass
from string.templatelib import Template
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pyodbc
import pytest
from fastapi import Body, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Integer, String, create_engine, event, inspect as sa_inspect, select, text, tstring, update
from sqlalchemy.engine import Engine, ExceptionContext, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from grad_pylib.core import db as db_module
from grad_pylib.core.config import BaseAppSettings
from grad_pylib.testing import ManagedE2eDatabases, SqlServerFixtureConfig, create_db_session_fixture
from grad_pylib.core.db import (
    DatabaseRuntime,
    NamedDatabases,
    ParsedSqlError,
    SqlServerErrorType,
    build_mssql_url,
    orm_upsert,
    parse_mssql_error,
    resolve_database_url,
    select_exclude,
)


class Base(DeclarativeBase):
    pass


class ExampleUpsertModel(Base):
    __tablename__ = "example_upsert_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    value: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(50))


class ExampleUpsertPayload(BaseModel):
    id: int
    value: str


class RenamedUpsertModel(Base):
    __tablename__ = "renamed_upsert_model"

    id: Mapped[int] = mapped_column("database_id", Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column("database_tenant_id", Integer, primary_key=True)
    value: Mapped[str] = mapped_column("database_value", String(50), nullable=False)
    created_by: Mapped[str | None] = mapped_column("database_created_by", String(50))


class RenamedUpsertPayload(ExampleUpsertPayload):
    tenant_id: int


class InsertOnlyPayload(BaseModel):
    created_by: str


class DummyDriverError(Exception):
    pass


class DummySession:
    def __init__(self) -> None:
        self.closed = False
        self.rolled_back = False

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        self.rolled_back = True


@dataclass
class RuntimeSpy:
    engine: object
    create_engine_calls: list[tuple[str, dict[str, object]]]
    sessionmaker_calls: list[dict[str, object]]
    sessions: list[DummySession]


@pytest.fixture(scope="module")
def db_engine(mssql_databases: ManagedE2eDatabases) -> Engine:
    Base.metadata.create_all(mssql_databases.app_engine)
    return mssql_databases.app_engine


@pytest.fixture
def session(db_engine: Engine) -> Generator[Session]:
    yield from create_db_session_fixture(db_engine, SqlServerFixtureConfig(
        migration_runner=Base.metadata.create_all,
        tables_to_clean=tuple(Base.metadata.tables),
    ))


@pytest.fixture
def runtime_spy(monkeypatch: pytest.MonkeyPatch) -> RuntimeSpy:
    create_engine_calls: list[tuple[str, dict[str, object]]] = []
    sessionmaker_calls: list[dict[str, object]] = []
    sessions: list[DummySession] = []
    # Never connected: DatabaseRuntime only needs an engine object to hand back.
    engine = create_engine("mssql+pyodbc://")

    def fake_create_engine(url: str, **kwargs: object) -> object:
        create_engine_calls.append((url, kwargs))
        return engine

    def fake_sessionmaker(**kwargs: object):
        sessionmaker_calls.append(kwargs)

        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)
    return RuntimeSpy(engine, create_engine_calls, sessionmaker_calls, sessions)


def make_sql_server_error(sql_state: str, driver_message: str) -> DBAPIError:
    return DBAPIError(None, None, DummyDriverError(sql_state, driver_message))


def sql_server_message(text: str, native_code: int) -> str:
    return f"{text} ({native_code}) (SQLExecDirectW)"


def test_build_mssql_url_round_trips_odbc_connection_string() -> None:
    connection_string = "Driver={ODBC Driver 18 for SQL Server};Server=localhost"

    url = build_mssql_url(connection_string)

    assert url.startswith("mssql+pyodbc://")
    assert make_url(url).query["odbc_connect"] == connection_string


def test_resolve_database_url_uses_database_url() -> None:
    url = resolve_database_url(
        BaseAppSettings(database_url="Driver={ODBC Driver 18 for SQL Server};Server=localhost")
    )

    assert make_url(url).query["odbc_connect"] == "Driver={ODBC Driver 18 for SQL Server};Server=localhost"


def test_resolve_database_url_requires_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must be set"):
        resolve_database_url(BaseAppSettings(database_url=None))


def test_database_runtime_reuses_engine_and_session_factory(runtime_spy: RuntimeSpy) -> None:
    runtime = DatabaseRuntime(
        lambda: "mssql+pyodbc://example",
        pool_pre_ping=False,
        pool_size=7,
        max_overflow=11,
        pool_recycle=60,
    )

    assert runtime.get_engine() is runtime_spy.engine
    assert runtime.get_engine() is runtime_spy.engine
    assert runtime_spy.create_engine_calls == [
        (
            "mssql+pyodbc://example",
            {"pool_pre_ping": False, "pool_size": 7, "max_overflow": 11, "pool_recycle": 60},
        )
    ]

    session_factory = runtime.get_session_factory()

    assert session_factory is runtime.get_session_factory()
    assert runtime_spy.sessionmaker_calls == [
        {
            "bind": runtime_spy.engine,
            "autoflush": False,
            "autocommit": False,
            "expire_on_commit": False,
        }
    ]


def test_database_runtime_disables_pyodbc_pooling_before_creating_the_engine(
        runtime_spy: RuntimeSpy,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pyodbc, "pooling", True)
    pooling_at_create: list[bool] = []
    spy_create_engine = db_module.create_engine

    def record_pooling(url: str, **kwargs: object) -> object:
        pooling_at_create.append(pyodbc.pooling)
        return spy_create_engine(url, **kwargs)

    monkeypatch.setattr(db_module, "create_engine", record_pooling)

    DatabaseRuntime(lambda: "mssql+pyodbc://example").get_engine()

    assert pooling_at_create == [False]


def test_database_runtime_session_closes_session(runtime_spy: RuntimeSpy) -> None:
    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")
    generator = runtime.session()
    session_obj = next(generator)

    assert session_obj is runtime_spy.sessions[0]
    assert not session_obj.closed

    with pytest.raises(StopIteration):
        next(generator)

    assert session_obj.closed


def test_database_runtime_session_closes_session_on_exception(runtime_spy: RuntimeSpy) -> None:
    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")
    generator = runtime.session()
    session_obj = next(generator)

    with pytest.raises(RuntimeError, match="boom"):
        generator.throw(RuntimeError("boom"))

    assert session_obj.closed


def test_database_runtime_background_session_closes_session(runtime_spy: RuntimeSpy) -> None:
    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")

    with runtime.background_session() as session_obj:
        assert session_obj is runtime_spy.sessions[0]
        assert not session_obj.closed

    assert session_obj.closed


def test_database_runtime_background_session_closes_session_on_exception(runtime_spy: RuntimeSpy) -> None:
    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")

    with pytest.raises(RuntimeError, match="boom"), runtime.background_session() as session_obj:
        raise RuntimeError("boom")

    assert session_obj.closed


def test_named_databases_registers_and_looks_up_runtimes(runtime_spy: RuntimeSpy) -> None:
    databases = NamedDatabases.from_settings(
        lambda: type(
            "Settings",
            (),
            {
                "database_url": "Driver={ODBC Driver 18 for SQL Server};Server=app",
                "codebook_database_url": "Driver={ODBC Driver 18 for SQL Server};Server=codebook",
            },
        )(),
        {"app": "database_url", "codebook": "codebook_database_url"},
        default_name="app",
        pool_size=7,
        max_overflow=11,
    )

    assert databases.names == ("app", "codebook")
    assert databases.default_name == "app"
    assert databases.default is databases["app"]
    assert databases.database(" app ") is databases["app"]
    assert databases.get_runtime() is databases["app"].runtime
    assert isinstance(databases["app"].runtime, DatabaseRuntime)

    app_engine = databases.get_engine()
    codebook_engine = databases["codebook"].get_engine()

    assert app_engine is databases["app"].get_engine()
    assert codebook_engine is databases["codebook"].get_engine()
    assert runtime_spy.create_engine_calls == [
        (
            build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=app"),
            {"pool_pre_ping": True, "pool_size": 7, "max_overflow": 11, "pool_recycle": 300},
        ),
        (
            build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=codebook"),
            {"pool_pre_ping": True, "pool_size": 7, "max_overflow": 11, "pool_recycle": 300},
        ),
    ]

    with pytest.raises(KeyError, match="Unknown database: 'missing'"):
        databases.database("missing")


def test_named_databases_requires_at_least_one_database() -> None:
    with pytest.raises(ValueError, match="At least one named database must be configured"):
        NamedDatabases({})


def test_named_databases_rejects_blank_names() -> None:
    with pytest.raises(ValueError, match="Database names must not be blank"):
        NamedDatabases({"   ": lambda: "mssql+pyodbc://example"})


def test_named_databases_rejects_duplicate_normalized_names() -> None:
    with pytest.raises(ValueError, match="Duplicate database name: app"):
        NamedDatabases(
            {
                "app": lambda: "mssql+pyodbc://app",
                " app ": lambda: "mssql+pyodbc://duplicate",
            }
        )


def test_named_databases_default_is_optional() -> None:
    databases = NamedDatabases({"app": lambda: "mssql+pyodbc://example"})

    assert databases.default_name is None

    with pytest.raises(ValueError, match="No default database configured"):
        _ = databases.default

    with pytest.raises(ValueError, match="No default database configured"):
        databases.get_runtime()


def test_named_databases_rejects_blank_default_name() -> None:
    with pytest.raises(ValueError, match="Default database name must not be blank"):
        NamedDatabases({"app": lambda: "mssql+pyodbc://example"}, default_name="  ")


def test_named_databases_rejects_unknown_default_name() -> None:
    with pytest.raises(KeyError, match="Unknown default database: 'missing'"):
        NamedDatabases({"app": lambda: "mssql+pyodbc://example"}, default_name="missing")


def test_named_databases_get_session_closes_session(runtime_spy: RuntimeSpy) -> None:
    databases = NamedDatabases({"app": lambda: "mssql+pyodbc://example"}, default_name="app")
    generator = databases.get_session()
    session_obj = next(generator)

    assert session_obj is runtime_spy.sessions[0]
    assert not session_obj.closed

    with pytest.raises(StopIteration):
        next(generator)

    assert session_obj.closed


def test_named_databases_get_background_session_closes_session(runtime_spy: RuntimeSpy) -> None:
    databases = NamedDatabases({"app": lambda: "mssql+pyodbc://example"}, default_name="app")

    with databases.get_background_session() as session_obj:
        assert session_obj is runtime_spy.sessions[0]
        assert not session_obj.closed

    assert session_obj.closed


@pytest.mark.parametrize("database_name", [None, "codebook"])
def test_named_databases_session_dependency_works_with_fastapi(
        runtime_spy: RuntimeSpy, database_name: str | None,
) -> None:
    databases = NamedDatabases(
        {"app": lambda: "mssql+pyodbc://app", "codebook": lambda: "mssql+pyodbc://codebook"},
        default_name="app",
    )
    DbSession = databases.session_dependency(database_name)
    app = FastAPI()

    @app.get("/")
    @app.get("/{name}")
    def read_root(session: DbSession) -> dict[str, bool]:
        return {"is_dummy_session": isinstance(session, DummySession)}

    with TestClient(app) as client:
        for url in ("/", "/?name=app", "/?name=codebook", "/?name=missing", "/missing"):
            response = client.get(url)
            assert response.status_code == 200
            assert response.json() == {"is_dummy_session": True}

    operation = app.openapi()["paths"]["/"]["get"]
    assert not operation.get("parameters")
    assert "requestBody" not in operation
    assert len(runtime_spy.create_engine_calls) == 1
    assert runtime_spy.create_engine_calls[0][0] == f"mssql+pyodbc://{database_name or 'app'}"
    assert all(session.closed for session in runtime_spy.sessions)


@pytest.mark.parametrize(
    ("database_name", "getter"),
    [
        (None, "get_runtime"),
        (None, "get_engine"),
        (None, "get_session"),
        (None, "get_background_session"),
        ("codebook", "get_engine"),
        ("codebook", "get_session"),
        ("codebook", "get_background_session"),
    ],
)
def test_database_getters_do_not_accept_request_parameters(
        runtime_spy: RuntimeSpy, getter: str, database_name: str | None,
) -> None:
    databases = NamedDatabases(
        {"app": lambda: "mssql+pyodbc://app", "codebook": lambda: "mssql+pyodbc://codebook"},
        default_name="app",
    )
    target = databases if database_name is None else databases[database_name]
    dependency = getattr(target, getter)
    DatabaseDependency = Annotated[Any, Depends(dependency)]
    app = FastAPI()

    @app.get("/")
    @app.get("/{name}")
    def read_root(value: DatabaseDependency) -> dict[str, bool]:
        if getter == "get_runtime":
            value.get_engine()
        elif getter == "get_background_session":
            with value as session:
                assert isinstance(session, DummySession)
        return {"resolved": True}

    with TestClient(app) as client:
        for url in ("/", "/?name=app", "/?name=codebook", "/?name=missing", "/missing"):
            response = client.get(url)
            assert response.status_code == 200
            assert response.json() == {"resolved": True}

    operation = app.openapi()["paths"]["/"]["get"]
    assert not operation.get("parameters")
    assert "requestBody" not in operation
    assert len(runtime_spy.create_engine_calls) == 1
    assert runtime_spy.create_engine_calls[0][0] == f"mssql+pyodbc://{database_name or 'app'}"
    assert all(session.closed for session in runtime_spy.sessions)


def test_orm_upsert_applies_insert_only_fields_on_insert(session: Session) -> None:
    record = orm_upsert(
        session,
        ExampleUpsertModel,
        {"id": 1, "value": "first"},
        insert_only={"created_by": "seed-user"},
    )

    assert record.value == "first"
    assert record.created_by == "seed-user"


def test_orm_upsert_preserves_insert_only_fields_and_primary_key_on_update(session: Session) -> None:
    orm_upsert(
        session,
        ExampleUpsertModel,
        {"id": 1, "value": "first"},
        insert_only={"created_by": "seed-user"},
    )

    record = orm_upsert(
        session,
        ExampleUpsertModel,
        {"id": 1, "value": "second"},
        insert_only={"created_by": "replacement-user"},
    )

    assert record.id == 1
    assert record.value == "second"
    assert record.created_by == "seed-user"


@pytest.mark.parametrize("replace_value", [False, True], ids=["refresh-only", "replace-stale-value"])
def test_orm_upsert_refreshes_existing_state_after_another_transaction(
        session: Session, db_engine: Engine, replace_value: bool,
) -> None:
    original = ExampleUpsertModel(id=1, value="original", created_by="seed-user")
    session.add(original)
    session.commit()
    assert not session.expire_on_commit

    with db_engine.begin() as competitor:
        competitor.execute(
            update(ExampleUpsertModel)
            .where(ExampleUpsertModel.id == 1)
            .values(value="competitor", created_by="competitor-user")
        )

    payload: dict[str, Any] = {"id": 1}
    if replace_value:
        payload["value"] = "original"
    with session.begin():
        record = orm_upsert(
            session, ExampleUpsertModel, payload, insert_only={"created_by": "insert-only-user"},
        )
        assert record is original
        assert record.value == ("original" if replace_value else "competitor")
        assert record.created_by == "competitor-user"

    with db_engine.connect() as observer:
        persisted = observer.execute(
            select(ExampleUpsertModel.value, ExampleUpsertModel.created_by).where(ExampleUpsertModel.id == 1)
        ).one()
    assert tuple(persisted) == (record.value, record.created_by)


def test_orm_upsert_preserves_pending_changes_and_leaves_rollback_to_the_caller(
        session: Session, db_engine: Engine,
) -> None:
    record = orm_upsert(
        session, ExampleUpsertModel, {"id": 1, "value": "original", "created_by": "seed-user"},
    )
    record.created_by = "pending-user"
    record.value = "pending-value"

    updated = orm_upsert(session, ExampleUpsertModel, {"id": 1, "value": "replacement"})

    assert updated is record
    assert updated.value == "replacement"
    assert updated.created_by == "pending-user"
    assert tuple(session.execute(
        select(ExampleUpsertModel.value, ExampleUpsertModel.created_by).where(ExampleUpsertModel.id == 1)
    ).one()) == ("replacement", "pending-user")
    with db_engine.connect() as observer:
        assert observer.execute(select(ExampleUpsertModel.id)).all() == []
    session.rollback()
    with db_engine.connect() as observer:
        assert observer.execute(select(ExampleUpsertModel.id)).all() == []


def test_orm_upsert_accepts_pydantic_model_input(session: Session) -> None:
    record = orm_upsert(
        session,
        ExampleUpsertModel,
        ExampleUpsertPayload(id=1, value="from-model"),
        insert_only={"created_by": "seed-user"},
    )

    assert record.id == 1
    assert record.value == "from-model"
    assert record.created_by == "seed-user"


def test_orm_upsert_rejects_duplicate_keys_between_data_source_and_insert_only(session: Session) -> None:
    with pytest.raises(
        ValueError,
        match="Duplicate keys provided in data_source and insert_only for ExampleUpsertModel: created_by",
    ):
        orm_upsert(
            session,
            ExampleUpsertModel,
            {"id": 1, "value": "first", "created_by": "from-data"},
            insert_only={"created_by": "seed-user"},
        )


def test_orm_upsert_requires_primary_key_values(session: Session) -> None:
    with pytest.raises(ValueError, match="Provided data missing primary key values for ExampleUpsertModel"):
        orm_upsert(session, ExampleUpsertModel, {"value": "missing-id"})


@pytest.mark.parametrize("data_factory", [dict, RenamedUpsertPayload, RenamedUpsertModel])
@pytest.mark.parametrize("insert_only_factory", [dict, InsertOnlyPayload, RenamedUpsertModel])
def test_orm_upsert_uses_mapped_attribute_names(
        session: Session,
        data_factory: Callable[..., dict[str, Any] | BaseModel | DeclarativeBase],
        insert_only_factory: Callable[..., dict[str, Any] | BaseModel | DeclarativeBase],
) -> None:
    record = orm_upsert(
        session,
        RenamedUpsertModel,
        data_factory(id=1, tenant_id=2, value="first"),
        insert_only=insert_only_factory(created_by="seed-user"),
    )
    other_record = orm_upsert(
        session, RenamedUpsertModel, data_factory(id=1, tenant_id=3, value="other"),
    )
    updated = orm_upsert(
        session,
        RenamedUpsertModel,
        data_factory(id=1, tenant_id=2, value="second"),
        insert_only=insert_only_factory(created_by="replacement-user"),
    )
    session.expire_all()

    assert updated is record
    assert (record.id, record.tenant_id) == (1, 2)
    assert record.value == "second"
    assert record.created_by == "seed-user"
    assert other_record.value == "other"


def test_orm_upsert_requires_all_renamed_primary_key_attributes(session: Session) -> None:
    with pytest.raises(ValueError, match="Provided data missing primary key values for RenamedUpsertModel"):
        orm_upsert(session, RenamedUpsertModel, {"id": 1, "value": "missing-tenant"})


def test_orm_upsert_rejects_duplicate_renamed_attributes(session: Session) -> None:
    with pytest.raises(ValueError, match="Duplicate keys provided.*: created_by"):
        orm_upsert(
            session,
            RenamedUpsertModel,
            {"id": 1, "tenant_id": 2, "value": "first", "created_by": "from-data"},
            insert_only={"created_by": "from-insert"},
        )


@pytest.mark.parametrize(
    "diagnostic_suffix",
    [
        "",
        "; [23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "The statement has been terminated. (3621)",
        "; [23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "The statement has been terminated. (3621) (SQLExecDirectW)  ",
    ],
)
@pytest.mark.parametrize(
    ("sql_state", "driver_message", "expected_type", "expected_code", "expected_idempotency"),
    [
        ("40001", sql_server_message("Transaction deadlocked", 1205), SqlServerErrorType.DEADLOCK, 1205, False),
        ("HYT00", sql_server_message("Lock request timeout", 1222), SqlServerErrorType.LOCK_TIMEOUT, 1222, False),
        ("40001", sql_server_message("Snapshot isolation conflict", 3960), SqlServerErrorType.RCSI_CONFLICT, 3960, False),
        (
            "23000",
            sql_server_message("Duplicate key on idempotent-marker row", 2627),
            SqlServerErrorType.DUPLICATE_KEY,
            2627,
            True,
        ),
        ("23000", sql_server_message("Duplicate key", 2601), SqlServerErrorType.DUPLICATE_KEY, 2601, False),
        ("23000", sql_server_message("Foreign key violation", 547), SqlServerErrorType.FOREIGN_KEY_VIOLATION, 547, False),
        (
            "23000",
            sql_server_message("The INSERT statement conflicted with the CHECK constraint 'CK_status'", 547),
            SqlServerErrorType.CHECK_CONSTRAINT_VIOLATION,
            547,
            False,
        ),
        ("23000", sql_server_message("Cannot insert NULL", 515), SqlServerErrorType.NOT_NULL_VIOLATION, 515, False),
        (
            "42000",
            "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "String or binary data would be truncated in table 'Hooding.dbo.student_institutional_overrides', "
            "column 'college_5'. Truncated value: 'EDUCf'. (2628) (SQLExecDirectW)",
            SqlServerErrorType.DATA_TRUNCATION,
            2628,
            False,
        ),
        ("22001", sql_server_message("String or binary data would be truncated", 8152), SqlServerErrorType.DATA_TRUNCATION, 8152, False),
        ("22007", sql_server_message("Conversion failed when converting date and/or time", 241), SqlServerErrorType.DATA_CONVERSION, 241, False),
        ("22018", sql_server_message("Conversion failed when converting the varchar value to data type int", 245), SqlServerErrorType.DATA_CONVERSION, 245, False),
        ("22018", sql_server_message("Error converting data type varchar to numeric", 8114), SqlServerErrorType.DATA_CONVERSION, 8114, False),
        ("22003", sql_server_message("Arithmetic overflow error converting expression", 8115), SqlServerErrorType.ARITHMETIC_OVERFLOW, 8115, False),
        ("22012", sql_server_message("Divide by zero error encountered", 8134), SqlServerErrorType.DIVIDE_BY_ZERO, 8134, False),
        ("42S22", sql_server_message("Invalid column name 'missing_column'", 207), SqlServerErrorType.INVALID_COLUMN, 207, False),
        ("42S02", sql_server_message("Invalid object name 'missing_table'", 208), SqlServerErrorType.INVALID_OBJECT, 208, False),
        ("42000", sql_server_message("The SELECT permission was denied on the object", 229), SqlServerErrorType.PERMISSION_DENIED, 229, False),
        ("28000", sql_server_message("Login failed for user 'app_user'", 18456), SqlServerErrorType.LOGIN_FAILED, 18456, False),
        ("42000", sql_server_message("Syntax error", 50000), SqlServerErrorType.UNKNOWN, 50000, False),
        ("23000", "Duplicate key (2627)", SqlServerErrorType.DUPLICATE_KEY, 2627, False),
        (
            "23000",
            sql_server_message("Duplicate key value is (1205)", 2627),
            SqlServerErrorType.DUPLICATE_KEY,
            2627,
            False,
        ),
    ],
)
def test_parse_mssql_error_classifies_sql_server_errors(
    sql_state: str,
    driver_message: str,
    expected_type: SqlServerErrorType,
    expected_code: int,
    expected_idempotency: bool,
    diagnostic_suffix: str,
) -> None:
    driver_message += diagnostic_suffix
    parsed = parse_mssql_error(
        make_sql_server_error(sql_state, driver_message),
        idempotency_markers=("idempotent-marker",),
    )

    assert parsed.error_type is expected_type
    assert parsed.native_code == expected_code
    assert parsed.driver_message == driver_message
    assert parsed.is_idempotency_hit is expected_idempotency


def test_parse_mssql_error_preserves_unknown_primary_diagnostic() -> None:
    message = (
        sql_server_message("Application error", 50000)
        + "; [40001] " + sql_server_message("Transaction deadlocked", 1205)
    )
    error = make_sql_server_error("42000", message)

    parsed = parse_mssql_error(error)

    assert parsed.error_type is SqlServerErrorType.UNKNOWN
    assert parsed.native_code == 50000
    assert not db_module._is_transient_conflict(error)


def test_parse_mssql_error_does_not_treat_message_values_as_native_codes() -> None:
    parsed = parse_mssql_error(make_sql_server_error("23000", "Invalid value (1205) in column"))

    assert parsed.error_type is SqlServerErrorType.UNKNOWN
    assert parsed.native_code is None


def test_parse_mssql_error_handles_malformed_driver_errors() -> None:
    parsed = parse_mssql_error(DBAPIError(None, None, DummyDriverError("23000")))

    assert parsed.error_type is SqlServerErrorType.UNKNOWN
    assert parsed.native_code == 0
    assert parsed.driver_message == ""
    assert parsed.is_idempotency_hit is False


def test_parsed_sql_error_identifies_data_validation_error_types() -> None:
    bad_data_types = {
        SqlServerErrorType.DUPLICATE_KEY,
        SqlServerErrorType.FOREIGN_KEY_VIOLATION,
        SqlServerErrorType.CHECK_CONSTRAINT_VIOLATION,
        SqlServerErrorType.NOT_NULL_VIOLATION,
        SqlServerErrorType.DATA_TRUNCATION,
        SqlServerErrorType.DATA_CONVERSION,
        SqlServerErrorType.ARITHMETIC_OVERFLOW,
    }

    identified_types = {
        error_type
        for error_type in SqlServerErrorType
        if ParsedSqlError(error_type, None, "", False).is_data_validation_error
    }

    assert identified_types == bad_data_types


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (make_sql_server_error("40001", sql_server_message("Transaction deadlocked", 1205)), True),
        (make_sql_server_error("HYT00", sql_server_message("Lock request timeout", 1222)), True),
        (make_sql_server_error("40001", sql_server_message("Snapshot isolation conflict", 3960)), True),
        (
            make_sql_server_error(
                "40001",
                sql_server_message("Transaction deadlocked", 1205)
                + "; [01000] The statement has been terminated. (3621)",
            ),
            True,
        ),
        (make_sql_server_error("23000", sql_server_message("Duplicate key", 2627)), False),
        (make_sql_server_error("23000", sql_server_message("Foreign key violation", 547)), False),
        (ValueError("not a db error"), False),
    ],
)
def test_is_transient_conflict_only_retries_known_transient_errors(
    error: BaseException,
    expected: bool,
) -> None:
    assert db_module._is_transient_conflict(error) is expected


LOST_CONNECTION_MESSAGE = "[Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x68 (104)"
TERMINATED_SESSION_MESSAGE = (
    "[HY000] [Microsoft][ODBC Driver 18 for SQL Server]Unspecified error occurred on SQL Server. "
    "Connection may have been terminated by the server. (0)"
)
BUSY_CONNECTION_MESSAGE = (
    "[HY000] [Microsoft][ODBC Driver 18 for SQL Server]Connection is busy with results for another command (0)"
)


def make_disconnect_error(driver_function: str, *, invalidated: bool = True) -> DBAPIError:
    return DBAPIError(
        "select 1",
        {},
        DummyDriverError("08S01", f"{LOST_CONNECTION_MESSAGE} ({driver_function})"),
        connection_invalidated=invalidated,
    )


@pytest.mark.parametrize(
    ("error", "disconnect", "retryable"),
    [
        (make_disconnect_error("SQLExecDirectW"), True, True),
        (make_disconnect_error("SQLDriverConnect"), True, True),
        (make_disconnect_error("SQLEndTran"), True, False),
        (make_disconnect_error("SQLExecDirectW", invalidated=False), False, False),
        (ValueError("not a db error"), False, False),
    ],
)
def test_disconnect_classification_only_retries_connections_lost_before_commit(
    error: BaseException,
    disconnect: bool,
    retryable: bool,
) -> None:
    assert db_module.is_disconnect_error(error) is disconnect
    assert db_module.is_retryable_disconnect(error) is retryable
    assert db_module._is_transient_conflict(error) is retryable


@pytest.mark.parametrize(
    ("driver_error", "is_disconnect"),
    [
        (DummyDriverError("HY000", f"{TERMINATED_SESSION_MESSAGE} (SQLFetch)"), True),
        (DummyDriverError("HY000", f"{TERMINATED_SESSION_MESSAGE} (SQLExecDirectW)"), True),
        (DummyDriverError("HY000", f"{BUSY_CONNECTION_MESSAGE} (SQLExecDirectW)"), False),
        (DummyDriverError("42000", f"{TERMINATED_SESSION_MESSAGE} (SQLExecDirectW)"), False),
        (DummyDriverError(20, b"Connection may have been terminated by the server"), False),
        (ValueError("HY000"), False),
    ],
    ids=["fetch", "execute", "busy", "other-sqlstate", "pymssql-args", "not-a-driver-error"],
)
def test_only_sessions_the_server_terminated_become_disconnects(
    driver_error: BaseException, is_disconnect: bool,
) -> None:
    context = SimpleNamespace(original_exception=driver_error, is_disconnect=False)

    db_module._detect_terminated_session(cast(ExceptionContext, context))

    assert context.is_disconnect is is_disconnect


def test_database_runtime_classifies_terminated_sessions_before_other_error_listeners() -> None:
    engine = DatabaseRuntime(lambda: "mssql+pyodbc://user:pass@localhost/app?driver=ODBC+Driver+18+for+SQL+Server").get_engine()
    try:
        assert list(cast(Any, engine.dialect.dispatch).handle_error) == [
            db_module._detect_terminated_session,
            db_module._flag_for_reset,
            db_module._log_disconnect,
        ]
    finally:
        engine.dispose()


def _batch_listener_counts(engine: Engine) -> tuple[int, int, int]:
    dispatch = cast(Any, engine.dispatch)
    return (
        len(dispatch.before_execute),
        len(dispatch.before_cursor_execute),
        len(dispatch.after_cursor_execute),
    )


def test_install_mssql_batch_support_is_idempotent() -> None:
    engine = create_engine("mssql+pyodbc://")

    db_module.install_mssql_batch_support(engine)
    db_module.install_mssql_batch_support(engine)

    assert _batch_listener_counts(engine) == (1, 1, 1)
    assert event.contains(engine, "after_cursor_execute", db_module._skip_leading_rowcounts)


def test_install_mssql_batch_support_only_validates_on_other_drivers() -> None:
    engine = create_engine("mssql+pymssql://")

    db_module.install_mssql_batch_support(engine)

    assert _batch_listener_counts(engine) == (1, 1, 0)


def test_database_runtime_installs_batch_support() -> None:
    engine = DatabaseRuntime(lambda: "mssql+pyodbc://localhost/app?driver=ODBC+Driver+18+for+SQL+Server").get_engine()
    try:
        assert _batch_listener_counts(engine) == (1, 1, 1)
        assert event.contains(engine, "before_execute", db_module._validate_batch_execution)
    finally:
        engine.dispose()


_NO_CURSOR: Any = None


def _dialect(name: str = "mssql", driver: str = "pyodbc") -> Any:
    return SimpleNamespace(dialect=SimpleNamespace(name=name, driver=driver))


@pytest.mark.parametrize(
    "statement",
    [
        text("SELECT 1"),
        tstring(Template("SELECT 1")),
        text("SELECT 1 AS id").columns(id=Integer),
    ],
    ids=["text", "tstring", "declared-columns"],
)
def test_batch_validation_accepts_textual_statements(statement: object) -> None:
    db_module._validate_batch_execution(_dialect(), statement, [], {}, {"mssql_skip_rowcounts": True})


@pytest.mark.parametrize(
    ("connection", "statement", "multiparams", "options", "message"),
    [
        (_dialect("postgresql", "psycopg"), text("SELECT 1"), [], {"mssql_skip_rowcounts": True}, "postgresql"),
        (_dialect("mssql", "pymssql"), text("SELECT 1"), [], {"mssql_skip_rowcounts": True}, "mssql\\+pymssql"),
        (_dialect(), select(ExampleUpsertModel), [], {"mssql_skip_rowcounts": True}, "not Select"),
        (_dialect(), text("SELECT :a"), [{"a": 1}, {"a": 2}], {"mssql_skip_rowcounts": True}, "executemany"),
        (_dialect(), text("SELECT 1"), [], {"mssql_skip_rowcounts": "true"}, "True or False"),
        (_dialect("postgresql", "psycopg"), select(ExampleUpsertModel), [], {"mssql_skip_rowcounts": 1}, "True or"),
    ],
    ids=["other-dialect", "other-driver", "orm-select", "executemany", "string-value", "integer-value"],
)
def test_batch_validation_rejects_unsupported_opted_in_use(
    connection: Any, statement: object, multiparams: list[dict[str, int]], options: dict[str, object], message: str,
) -> None:
    with pytest.raises(ArgumentError, match=message):
        db_module._validate_batch_execution(connection, statement, multiparams, {}, options)


@pytest.mark.parametrize("options", [{}, {"mssql_skip_rowcounts": False}], ids=["absent", "false"])
def test_batch_validation_ignores_executions_without_the_option(options: dict[str, object]) -> None:
    db_module._validate_batch_execution(
        _dialect("postgresql", "psycopg"), select(ExampleUpsertModel), [{"a": 1}, {"a": 2}], {}, options,
    )


@pytest.mark.parametrize(
    ("connection", "compiled", "executemany", "message"),
    [
        (_dialect("mssql", "pymssql"), None, False, "mssql\\+pymssql"),
        (_dialect(), None, True, "executemany"),
    ],
    ids=["other-driver", "executemany"],
)
def test_driver_sql_validation_rejects_unsupported_opted_in_use(
    connection: Any, compiled: object, executemany: bool, message: str,
) -> None:
    context: Any = SimpleNamespace(compiled=compiled, execution_options={"mssql_skip_rowcounts": True})

    with pytest.raises(ArgumentError, match=message):
        db_module._validate_driver_sql_batch(connection, _NO_CURSOR, "SELECT 1", (), context, executemany)


def test_driver_sql_validation_leaves_compiled_statements_to_before_execute() -> None:
    context: Any = SimpleNamespace(compiled=object(), execution_options={"mssql_skip_rowcounts": True})

    db_module._validate_driver_sql_batch(_dialect("mssql", "pymssql"), _NO_CURSOR, "SELECT 1", (), context, True)


class _BatchCursor:
    """Cursor double whose results are column descriptions, with None for a row count."""

    def __init__(self, *descriptions: tuple[str, ...] | None) -> None:
        self._descriptions = descriptions
        self.index = 0

    @property
    def description(self) -> tuple[str, ...] | None:
        return self._descriptions[self.index] if self.index < len(self._descriptions) else None

    def nextset(self) -> bool:
        self.index += 1
        return self.index < len(self._descriptions)

    def fetchall(self) -> list[object]:
        raise AssertionError("navigation must not fetch rows")


@pytest.mark.parametrize(
    ("descriptions", "final_index"),
    [
        ((None, None, ("id",), None, ("b",)), 2),
        ((("id",), None), 0),
        ((None, None), 2),
        ((None,), 1),
    ],
    ids=["leading-row-counts", "leading-rowset", "row-counts-only", "single-row-count"],
)
def test_navigation_stops_at_the_first_rowset_or_exhaustion(
    descriptions: tuple[tuple[str, ...] | None, ...], final_index: int,
) -> None:
    cursor = _BatchCursor(*descriptions)
    context: Any = SimpleNamespace(execution_options={"mssql_skip_rowcounts": True})

    db_module._skip_leading_rowcounts(_dialect(), cast(Any, cursor), "", (), context, False)

    assert cursor.index == final_index


@pytest.mark.parametrize("options", [{}, {"mssql_skip_rowcounts": False}], ids=["absent", "false"])
def test_navigation_leaves_cursors_alone_without_the_option(options: dict[str, object]) -> None:
    cursor = _BatchCursor(None, ("id",))
    context: Any = SimpleNamespace(execution_options=options)

    db_module._skip_leading_rowcounts(_dialect(), cast(Any, cursor), "", (), context, False)

    assert cursor.index == 0


def test_describe_disconnect_reports_driver_fields_without_statement() -> None:
    fields = db_module.describe_disconnect(make_disconnect_error("SQLEndTran"))

    assert fields == {
        "sqlstate": "08S01",
        "driver_message": f"{LOST_CONNECTION_MESSAGE} (SQLEndTran)",
        "during_commit": True,
    }


class LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, /, **fields: object) -> None:
        self.events.append(("warning", event, fields))

    def info(self, event: str, /, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for _, name, fields in self.events if name == event]


@pytest.fixture
def db_logs(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    recorder = LogRecorder()
    monkeypatch.setattr(db_module, "_logger", recorder)
    return recorder


@dataclass
class FlakyEngine:
    """SQL Server engine whose pyodbc connection is lost on demand."""
    engine: Engine
    statement_failures: int = 0
    commit_failures: int = 0


def lost_connection(driver_function: str) -> pyodbc.OperationalError:
    """Build the error pyodbc raises when SQL Server drops the connection."""
    return pyodbc.OperationalError("08S01", f"{LOST_CONNECTION_MESSAGE} ({driver_function})")


@pytest.fixture
def flaky_engine(monkeypatch: pytest.MonkeyPatch, db_engine: Engine) -> Generator[FlakyEngine]:
    engine = create_engine(db_engine.url)
    db_module._install_disconnect_logging(engine)
    # Connect once so the dialect's first-connect initialization cannot consume a failure.
    with engine.connect():
        pass
    flaky = FlakyEngine(engine)
    dialect = engine.dialect
    real_execute = dialect.do_execute
    real_commit = dialect.do_commit

    def do_execute(cursor: Any, statement: str, parameters: Any, context: Any = None) -> None:
        if flaky.statement_failures:
            flaky.statement_failures -= 1
            raise lost_connection("SQLExecDirectW")
        real_execute(cursor, statement, parameters, context)

    def do_commit(dbapi_connection: Any) -> None:
        if flaky.commit_failures:
            flaky.commit_failures -= 1
            raise lost_connection("SQLEndTran")
        real_commit(dbapi_connection)

    monkeypatch.setattr(dialect, "do_execute", do_execute)
    monkeypatch.setattr(dialect, "do_commit", do_commit)
    yield flaky
    engine.dispose()


def test_retry_on_transient_conflict_retries_statement_lost_connection_on_fresh_connection(
    flaky_engine: FlakyEngine,
    db_logs: LogRecorder,
) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> int:
        attempts.append(1)
        return session.execute(text("select 1")).scalar_one()

    flaky_engine.statement_failures = 1
    with Session(flaky_engine.engine) as session:
        assert write(session) == 1

    assert len(attempts) == 2
    [disconnect] = db_logs.named("db.disconnect")
    assert disconnect["during_commit"] is False
    assert isinstance(disconnect["connection_age_seconds"], float)
    [retry_log] = db_logs.named("db.retry")
    assert retry_log["reason"] == "connection_lost"
    assert retry_log["attempt"] == 1
    assert retry_log["sqlstate"] == "08S01"
    assert str(retry_log["operation"]).endswith("write")


def test_retry_on_transient_conflict_reraises_connection_lost_during_commit(
    flaky_engine: FlakyEngine,
    db_logs: LogRecorder,
) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        attempts.append(1)
        session.execute(text("select 1"))

    flaky_engine.commit_failures = 1
    with Session(flaky_engine.engine) as session, pytest.raises(DBAPIError) as raised:
        write(session)

    assert raised.value.connection_invalidated
    assert len(attempts) == 1
    assert db_logs.named("db.retry") == []
    [disconnect] = db_logs.named("db.disconnect")
    assert disconnect["during_commit"] is True


def deadlock() -> DBAPIError:
    return make_sql_server_error("40001", sql_server_message("Transaction deadlocked", 1205))


def stored_values(session: Session) -> list[str]:
    with session.begin():
        return list(session.scalars(select(ExampleUpsertModel.value).order_by(ExampleUpsertModel.id)))


def test_retry_on_transient_conflict_logs_contention_retries(session: Session, db_logs: LogRecorder) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise deadlock()
        return "done"

    assert write(session) == "done"
    [retry_log] = db_logs.named("db.retry")
    assert retry_log["reason"] == "deadlock"
    assert retry_log["native_code"] == 1205
    assert str(retry_log["operation"]).endswith("write")
    assert "driver_message" not in retry_log


def test_retry_on_transient_conflict_commits_only_the_successful_attempt(session: Session) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> ExampleUpsertModel:
        attempts.append(1)
        row = ExampleUpsertModel(id=len(attempts), value=f"attempt {len(attempts)}")
        session.add(row)
        session.flush()
        if len(attempts) == 1:
            raise deadlock()
        return row

    row = write(session)

    assert len(attempts) == 2
    assert not session.in_transaction()
    assert row.value == "attempt 2"
    assert stored_values(session) == ["attempt 2"]


def test_retry_on_transient_conflict_accepts_the_session_as_a_keyword_argument(session: Session) -> None:
    @db_module.retry_on_transient_conflict
    def write(value: str, *, session: Session) -> None:
        session.add(ExampleUpsertModel(id=1, value=value))

    write("kept", session=session)

    assert stored_values(session) == ["kept"]


def test_retry_on_transient_conflict_allows_queries_after_the_call(session: Session) -> None:
    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        session.add(ExampleUpsertModel(id=1, value="written"))

    write(session)

    assert session.scalars(select(ExampleUpsertModel.value)).all() == ["written"]
    session.rollback()
    assert stored_values(session) == ["written"]


def test_retry_on_transient_conflict_reraises_after_the_final_attempt(session: Session) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        attempts.append(1)
        session.add(ExampleUpsertModel(id=len(attempts), value="lost"))
        session.flush()
        raise deadlock()

    with pytest.raises(DBAPIError):
        write(session)

    assert len(attempts) == 3
    assert not session.in_transaction()
    assert stored_values(session) == []


def test_retry_on_transient_conflict_rolls_back_without_retrying_other_errors(session: Session) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        attempts.append(1)
        session.add(ExampleUpsertModel(id=1, value="lost"))
        session.flush()
        raise ValueError("invalid")

    with pytest.raises(ValueError, match="invalid"):
        write(session)

    assert len(attempts) == 1
    assert not session.in_transaction()
    assert stored_values(session) == []


@pytest.mark.parametrize("begin_transaction", [
    pytest.param(lambda session: session.execute(text("select 1")), id="autobegun-by-read"),
    pytest.param(lambda session: session.begin(), id="explicit-begin"),
])
def test_retry_on_transient_conflict_rejects_a_transaction_in_progress(
    session: Session,
    begin_transaction: Callable[[Session], object],
) -> None:
    attempts: list[int] = []

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        attempts.append(1)

    begin_transaction(session)
    with pytest.raises(RuntimeError, match="must own its transaction"):
        write(session)

    assert attempts == []
    assert session.in_transaction()


def test_retry_on_transient_conflict_rejects_a_transaction_from_the_callers_write(session: Session) -> None:
    session.add(ExampleUpsertModel(id=1, value="caller"))
    session.flush()

    @db_module.retry_on_transient_conflict
    def write(session: Session) -> None:
        session.add(ExampleUpsertModel(id=2, value="decorated"))

    with pytest.raises(RuntimeError, match="must own its transaction"):
        write(session)

    session.commit()
    assert stored_values(session) == ["caller"]


def test_retry_on_transient_conflict_rejects_nested_decorated_calls(session: Session) -> None:
    @db_module.retry_on_transient_conflict
    def inner(session: Session) -> None:
        session.add(ExampleUpsertModel(id=2, value="inner"))

    @db_module.retry_on_transient_conflict
    def outer(session: Session) -> None:
        session.add(ExampleUpsertModel(id=1, value="outer"))
        inner(session)

    with pytest.raises(RuntimeError, match="inner must own its transaction"):
        outer(session)

    assert not session.in_transaction()
    assert stored_values(session) == []


def test_retry_on_transient_conflict_requires_exactly_one_session(session: Session) -> None:
    @db_module.retry_on_transient_conflict
    def write(*sessions: Session) -> None:
        pass

    with pytest.raises(TypeError, match="exactly one Session argument .*; got 0"):
        write()
    with pytest.raises(TypeError, match="exactly one Session argument .*; got 2"):
        write(session, session)


def test_retry_on_transient_conflict_rejects_coroutine_functions() -> None:
    async def write(session: Session) -> None:
        pass

    with pytest.raises(TypeError, match="synchronous functions only"):
        db_module.retry_on_transient_conflict(write)


def test_retry_on_disconnect_runs_again_on_a_fresh_connection(
    flaky_engine: FlakyEngine,
    db_logs: LogRecorder,
) -> None:
    attempts: list[int] = []

    @db_module.retry_on_disconnect
    def read(session: Session) -> int:
        attempts.append(1)
        return session.execute(text("select 1")).scalar_one()

    flaky_engine.statement_failures = 1
    with Session(flaky_engine.engine) as session:
        assert read(session) == 1

    assert len(attempts) == 2
    assert [event for _, event, _ in db_logs.events] == ["db.disconnect", "db.retry", "db.retry.succeeded"]
    [retry_log] = db_logs.named("db.retry")
    assert retry_log["reason"] == "connection_lost"
    assert retry_log["during_commit"] is False
    assert str(retry_log["operation"]).endswith("read")


def test_retry_on_disconnect_rolls_back_every_session_argument(flaky_engine: FlakyEngine, db_engine: Engine) -> None:
    attempts: list[int] = []

    @db_module.retry_on_disconnect
    def read(session: Session, *, other_session: Session) -> int:
        attempts.append(1)
        session.execute(text("select 1"))
        return other_session.execute(text("select 2")).scalar_one()

    with Session(db_engine) as session, Session(flaky_engine.engine) as other_session:
        # Fail the second session's statement, after the first session has begun a transaction.
        flaky_engine.statement_failures = 1
        assert read(session, other_session=other_session) == 2

    assert len(attempts) == 2


def test_retry_on_disconnect_reraises_a_second_lost_connection(
    flaky_engine: FlakyEngine,
    db_logs: LogRecorder,
) -> None:
    attempts: list[int] = []

    @db_module.retry_on_disconnect
    def read(session: Session) -> None:
        attempts.append(1)
        session.execute(text("select 1"))

    flaky_engine.statement_failures = 2
    with Session(flaky_engine.engine) as session, pytest.raises(DBAPIError) as raised:
        read(session)

    assert raised.value.connection_invalidated
    assert len(attempts) == 2
    assert db_logs.named("db.retry.succeeded") == []


@pytest.mark.parametrize(
    "error",
    [
        make_disconnect_error("SQLEndTran"),
        make_sql_server_error("40001", sql_server_message("Transaction deadlocked", 1205)),
    ],
)
def test_retry_on_disconnect_does_not_retry_other_errors(error: DBAPIError, db_logs: LogRecorder) -> None:
    attempts: list[int] = []

    @db_module.retry_on_disconnect
    def read() -> None:
        attempts.append(1)
        raise error

    with pytest.raises(DBAPIError):
        read()

    assert len(attempts) == 1
    assert db_logs.events == []


def test_retry_on_disconnect_rejects_async_functions() -> None:
    async def read() -> None:
        pass

    with pytest.raises(TypeError, match="synchronous functions only"):
        db_module.retry_on_disconnect(read)


def test_retry_on_disconnect_keeps_fastapi_endpoint_parameters(flaky_engine: FlakyEngine) -> None:
    def get_session() -> Generator[Session]:
        with Session(flaky_engine.engine) as session:
            yield session

    app = FastAPI()

    @app.post("/search")
    @db_module.retry_on_disconnect
    def search(session: Annotated[Session, Depends(get_session)], query: Annotated[str, Body(embed=True)]) -> dict[str, Any]:
        return {"query": query, "value": session.execute(text("select 1")).scalar_one()}

    flaky_engine.statement_failures = 1
    response = TestClient(app).post("/search", json={"query": "smith"})

    assert response.status_code == 200
    assert response.json() == {"query": "smith", "value": 1}


def test_disconnect_logging_reports_pre_ping_failures_at_info(
    monkeypatch: pytest.MonkeyPatch,
    db_logs: LogRecorder,
    db_engine: Engine,
) -> None:
    engine = create_engine(db_engine.url, pool_pre_ping=True)
    db_module._install_disconnect_logging(engine)
    with engine.connect() as connection:
        connection.execute(text("select 1"))

    ping_failures = [1]
    real_ping = engine.dialect.do_ping

    def ping_once_failing(dbapi_connection: Any) -> bool:
        if ping_failures:
            ping_failures.pop()
            raise lost_connection("SQLExecDirectW")
        return real_ping(dbapi_connection)

    monkeypatch.setattr(engine.dialect, "do_ping", ping_once_failing)
    with engine.connect() as connection:
        connection.execute(text("select 1"))
    engine.dispose()

    assert db_logs.events == [
        (
            "info",
            "db.disconnect.pre_ping",
            {"sqlstate": "08S01", "driver_message": f"{LOST_CONNECTION_MESSAGE} (SQLExecDirectW)", "during_commit": False},
        ),
    ]


def test_disconnect_logging_does_not_reconnect_an_invalidated_connection(
    monkeypatch: pytest.MonkeyPatch,
    db_logs: LogRecorder,
    db_engine: Engine,
) -> None:
    engine = create_engine(db_engine.url)
    db_module._install_disconnect_logging(engine)
    connect_attempts: list[None] = []

    def failing_connect(*_args: Any, **_kwargs: Any) -> None:
        connect_attempts.append(None)
        raise lost_connection("SQLDriverConnect")

    with engine.connect() as connection:
        connection.execute(text("select 1"))
        connection.invalidate()
        connection.rollback()
        monkeypatch.setattr(engine.dialect, "connect", failing_connect)

        with pytest.raises(DBAPIError):
            connection.execute(text("select 1"))
    engine.dispose()

    assert len(connect_attempts) == 1
    [fields] = db_logs.named("db.disconnect")
    assert fields["connection_age_seconds"] is None
    assert fields["sqlstate"] == "08S01"


def test_select_exclude_omits_columns_from_table_select(session: Session) -> None:
    session.add(ExampleUpsertModel(id=1, value="first", created_by="seed-user"))
    session.commit()

    row = session.execute(select_exclude(ExampleUpsertModel.__table__, {"created_by"})).one()

    assert set(row._mapping) == {"id", "value"}
    assert row.id == 1
    assert row.value == "first"


def test_select_exclude_defers_excluded_columns_for_orm_models(session: Session) -> None:
    session.add(ExampleUpsertModel(id=1, value="first", created_by="seed-user"))
    session.commit()

    read_session_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    with read_session_factory() as read_session:
        record = read_session.execute(select_exclude(ExampleUpsertModel, {"created_by"})).scalar_one()

        assert record.id == 1
        assert record.value == "first"
        assert "created_by" in sa_inspect(record).unloaded


@pytest.mark.parametrize("use_table", [False, True])
def test_select_exclude_rejects_excluding_all_columns(use_table: bool) -> None:
    model_or_table = ExampleUpsertModel.__table__ if use_table else ExampleUpsertModel

    with pytest.raises(ValueError, match="select_exclude must retain at least one column"):
        select_exclude(model_or_table, {"id", "value", "created_by"})


@pytest.mark.parametrize("use_table", [False, True])
@pytest.mark.parametrize("exclude", [set(), {"missing"}, {"value", "created_by"}])
def test_select_exclude_retains_available_columns(
        session: Session, use_table: bool, exclude: set[str],
) -> None:
    session.add(ExampleUpsertModel(id=1, value="first", created_by="seed-user"))
    session.commit()
    session.expunge_all()
    model_or_table = ExampleUpsertModel.__table__ if use_table else ExampleUpsertModel

    row = session.execute(select_exclude(model_or_table, exclude)).one()

    if use_table:
        assert set(row._mapping) == {"id", "value", "created_by"} - exclude
        assert row.id == 1
    else:
        record = row[0]
        assert record.id == 1
        assert sa_inspect(record).unloaded == exclude & {"value", "created_by"}
