from collections.abc import Generator
from typing import get_args, get_origin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from grad_pylib.core.config import BaseAppSettings
from grad_pylib.core import db as db_module
from grad_pylib.core.db import DatabaseRuntime, NamedDatabases, build_mssql_url, orm_upsert, resolve_database_url


class Base(DeclarativeBase):
    pass


class ExampleUpsertModel(Base):
    __tablename__ = "example_upsert_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(50))


@pytest.fixture
def session() -> Generator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db_session:
        yield db_session

    engine.dispose()


def test_build_mssql_url_uses_odbc_connect_query() -> None:
    url = build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=localhost")
    assert url.startswith("mssql+pyodbc://")
    assert "odbc_connect=" in url


def test_resolve_database_url_requires_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must be set"):
        resolve_database_url(BaseAppSettings(database_url=None))


def test_orm_upsert_applies_insert_only_fields_on_insert(session: Session) -> None:
    record = orm_upsert(
        session,
        ExampleUpsertModel,
        {"id": 1, "value": "first"},
        insert_only={"created_by": "seed-user"},
    )

    assert record.value == "first"
    assert record.created_by == "seed-user"


def test_orm_upsert_preserves_insert_only_fields_on_update(session: Session) -> None:
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

    assert record.value == "second"
    assert record.created_by == "seed-user"


class DummySession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_database_runtime_reuses_engine_and_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    create_engine_calls: list[tuple[str, dict[str, object]]] = []
    sessionmaker_calls: list[dict[str, object]] = []
    engine = object()

    def fake_create_engine(url: str, **kwargs: object) -> object:
        create_engine_calls.append((url, kwargs))
        return engine

    def fake_sessionmaker(**kwargs: object):
        sessionmaker_calls.append(kwargs)

        def factory() -> DummySession:
            return DummySession()

        return factory

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")

    assert runtime.get_engine() is engine
    assert runtime.get_engine() is engine
    assert len(create_engine_calls) == 1

    session_factory = runtime.get_session_factory()
    assert session_factory is runtime.get_session_factory()
    assert len(sessionmaker_calls) == 1


def test_database_runtime_session_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[DummySession] = []

    monkeypatch.setattr(db_module, "create_engine", lambda *_args, **_kwargs: object())

    def fake_sessionmaker(**_kwargs: object):
        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")
    generator = runtime.session()
    session_obj = next(generator)

    assert session_obj is sessions[0]
    assert not session_obj.closed

    with pytest.raises(StopIteration):
        next(generator)

    assert session_obj.closed


def test_database_runtime_background_session_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[DummySession] = []

    monkeypatch.setattr(db_module, "create_engine", lambda *_args, **_kwargs: object())

    def fake_sessionmaker(**_kwargs: object):
        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    runtime = DatabaseRuntime(lambda: "mssql+pyodbc://example")
    with runtime.background_session() as session_obj:
        assert session_obj is sessions[0]
        assert not session_obj.closed

    assert session_obj.closed


def test_named_databases_registers_and_looks_up_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    create_engine_calls: list[str] = []

    def fake_create_engine(url: str, **_kwargs: object) -> object:
        create_engine_calls.append(url)
        return object()

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)

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
        pool_size=7,
        max_overflow=11,
    )

    assert databases.names == ("app", "codebook")
    assert databases["app"] is databases.database("app")
    assert isinstance(databases.get_runtime("app"), DatabaseRuntime)

    app_engine = databases["app"].get_engine()
    codebook_engine = databases["codebook"].get_engine()

    assert app_engine is databases["app"].get_engine()
    assert codebook_engine is databases["codebook"].get_engine()
    assert create_engine_calls == [
        build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=app"),
        build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=codebook"),
    ]

    with pytest.raises(KeyError, match="Unknown database: 'missing'"):
        databases.database("missing")


def test_named_databases_get_session_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[DummySession] = []

    monkeypatch.setattr(db_module, "create_engine", lambda *_args, **_kwargs: object())

    def fake_sessionmaker(**_kwargs: object):
        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    app_db = NamedDatabases({"app": lambda: "mssql+pyodbc://example"})["app"]
    generator = app_db.get_session()
    session_obj = next(generator)

    assert session_obj is sessions[0]
    assert not session_obj.closed

    with pytest.raises(StopIteration):
        next(generator)

    assert session_obj.closed


def test_named_databases_get_background_session_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[DummySession] = []

    monkeypatch.setattr(db_module, "create_engine", lambda *_args, **_kwargs: object())

    def fake_sessionmaker(**_kwargs: object):
        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    app_db = NamedDatabases({"app": lambda: "mssql+pyodbc://example"})["app"]
    with app_db.get_background_session() as session_obj:
        assert session_obj is sessions[0]
        assert not session_obj.closed

    assert session_obj.closed


def test_named_databases_session_dependency_builds_fastapi_annotation() -> None:
    app_db = NamedDatabases({"app": lambda: "mssql+pyodbc://example"})["app"]

    annotation = app_db.session_dependency()
    session_type, dependency = get_args(annotation)
    dependency_callable = dependency.dependency

    assert get_origin(annotation) is not None
    assert session_type is Session
    assert dependency_callable is not None
    assert dependency_callable.__self__ is app_db
    assert dependency_callable.__func__ is app_db.get_session.__func__


def test_named_databases_session_dependency_works_with_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[DummySession] = []

    monkeypatch.setattr(db_module, "create_engine", lambda *_args, **_kwargs: object())

    def fake_sessionmaker(**_kwargs: object):
        def factory() -> DummySession:
            session = DummySession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(db_module, "sessionmaker", fake_sessionmaker)

    app_db = NamedDatabases({"app": lambda: "mssql+pyodbc://example"})["app"]
    DbSession = app_db.session_dependency()
    app = FastAPI()

    @app.get("/")
    def read_root(session: DbSession) -> dict[str, bool]:
        return {"is_dummy_session": isinstance(session, DummySession)}

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"is_dummy_session": True}
    assert sessions[0].closed
