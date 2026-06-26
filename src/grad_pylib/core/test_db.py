from collections.abc import Generator

import pytest
from sqlalchemy import Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from grad_pylib.core.config import BaseAppSettings
from grad_pylib.core import db as db_module
from grad_pylib.core.db import DatabaseRuntime, build_mssql_url, orm_upsert, resolve_database_url


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
