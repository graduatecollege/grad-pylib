from collections.abc import Generator
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Integer, String, create_engine, inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from grad_pylib.core import db as db_module
from grad_pylib.core.config import BaseAppSettings
from grad_pylib.core.db import (
    DatabaseRuntime,
    NamedDatabases,
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(50))


class ExampleUpsertPayload(BaseModel):
    id: int
    value: str


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


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db_session:
        yield db_session

    engine.dispose()


@pytest.fixture
def runtime_spy(monkeypatch: pytest.MonkeyPatch) -> RuntimeSpy:
    create_engine_calls: list[tuple[str, dict[str, object]]] = []
    sessionmaker_calls: list[dict[str, object]] = []
    sessions: list[DummySession] = []
    engine = object()

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
    )

    assert runtime.get_engine() is runtime_spy.engine
    assert runtime.get_engine() is runtime_spy.engine
    assert runtime_spy.create_engine_calls == [
        (
            "mssql+pyodbc://example",
            {"pool_pre_ping": False, "pool_size": 7, "max_overflow": 11},
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
            {"pool_pre_ping": True, "pool_size": 7, "max_overflow": 11},
        ),
        (
            build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=codebook"),
            {"pool_pre_ping": True, "pool_size": 7, "max_overflow": 11},
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
        ("23000", sql_server_message("Cannot insert NULL", 515), SqlServerErrorType.NOT_NULL_VIOLATION, 515, False),
        ("42000", sql_server_message("Syntax error", 50000), SqlServerErrorType.UNKNOWN, 50000, False),
    ],
)
def test_parse_mssql_error_classifies_sql_server_errors(
    sql_state: str,
    driver_message: str,
    expected_type: SqlServerErrorType,
    expected_code: int,
    expected_idempotency: bool,
) -> None:
    parsed = parse_mssql_error(
        make_sql_server_error(sql_state, driver_message),
        idempotency_markers=("idempotent-marker",),
    )

    assert parsed.error_type is expected_type
    assert parsed.native_code == expected_code
    assert parsed.driver_message == driver_message
    assert parsed.is_idempotency_hit is expected_idempotency


def test_parse_mssql_error_handles_malformed_driver_errors() -> None:
    parsed = parse_mssql_error(DBAPIError(None, None, DummyDriverError("23000")))

    assert parsed.error_type is SqlServerErrorType.UNKNOWN
    assert parsed.native_code == 0
    assert parsed.driver_message == ""
    assert parsed.is_idempotency_hit is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (make_sql_server_error("40001", sql_server_message("Transaction deadlocked", 1205)), True),
        (make_sql_server_error("HYT00", sql_server_message("Lock request timeout", 1222)), True),
        (make_sql_server_error("40001", sql_server_message("Snapshot isolation conflict", 3960)), True),
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
