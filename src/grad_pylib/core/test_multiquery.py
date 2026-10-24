import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pyodbc
import pytest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Column, Executable, Integer, MetaData, String, Table, create_engine, literal, select, text
from sqlalchemy.engine import Connection, CursorResult, Engine, Result
from sqlalchemy.exc import ArgumentError, DataError, DBAPIError
from sqlalchemy.orm import DeclarativeBase

from grad_pylib.core import multiquery as multiquery_module
from grad_pylib.core.db import SqlServerErrorType, parse_mssql_error
from grad_pylib.core.multiquery import (
    cursor_rows_to_dicts,
    execute_script_batch,
    map_row_to_pydantic,
    qualified_columns,
    read_all_result_sets,
    section_columns,
    split_row_sections,
)
from grad_pylib.testing import ManagedE2eDatabases


metadata = MetaData()
parent = Table(
    "parent",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("name", String, nullable=False),
)
child = Table(
    "child",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("parent_id", Integer, nullable=False),
    Column("name", String, nullable=False),
)
renamed = Table(
    "renamed",
    metadata,
    Column("db_id", Integer, key="python_id", primary_key=True, autoincrement=False),
    Column("db_name", String, key="python_name"),
)


class _Base(DeclarativeBase):
    pass


class _RenamedModel(_Base):
    __table__ = renamed


class _ChildDto(BaseModel):
    id: int
    name: str


class _ParentDto(BaseModel):
    id: int
    display_name: str
    child: _ChildDto | None


class _ChildOnlyDto(BaseModel):
    parent_id: int | None = None
    child: _ChildDto | None


class _RootDto(BaseModel):
    total: int


class _RequiredChildDto(BaseModel):
    child: _ChildDto


class _AlternativeChildDto(BaseModel):
    id: int
    name: str


class _AmbiguousDto(BaseModel):
    child: _ChildDto | _AlternativeChildDto


class _OptionalAmbiguousDto(BaseModel):
    child: _ChildDto | _AlternativeChildDto | None


class _RenamedDto(BaseModel):
    python_id: int = Field(validation_alias="db_id")
    python_name: str = Field(validation_alias="db_name")


class _NestedRenamedDto(BaseModel):
    record: _RenamedDto


type _CursorSet = tuple[tuple[str, ...] | None, list[tuple[object, ...]] | BaseException]


@dataclass
class _Cursor:
    """A DB-API cursor double; an exception entry fails ``nextset``, exception rows fail ``fetchall``."""

    result_sets: tuple[_CursorSet | BaseException, ...]
    index: int = 0
    closes: int = 0
    fetches: list[int] = field(default_factory=list)
    close_error: BaseException | None = None

    @property
    def closed(self) -> bool:
        return self.closes > 0

    def _current(self) -> _CursorSet:
        result_set = self.result_sets[self.index]
        assert not isinstance(result_set, BaseException)
        return result_set

    @property
    def description(self) -> tuple[tuple[str], ...] | None:
        names, _ = self._current()
        return tuple((name,) for name in names) if names is not None else None

    def fetchall(self) -> list[tuple[object, ...]]:
        self.fetches.append(self.index)
        rows = self._current()[1]
        if isinstance(rows, BaseException):
            raise rows
        return rows

    def nextset(self) -> bool | None:
        if self.index + 1 == len(self.result_sets):
            return None
        self.index += 1
        result_set = self.result_sets[self.index]
        if isinstance(result_set, BaseException):
            self.result_sets = self.result_sets[:self.index]
            self.index -= 1
            raise result_set
        return True

    def close(self) -> None:
        # pyodbc rejects closing a cursor twice, so every close is counted.
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture(scope="module")
def multiquery_engine(mssql_databases: ManagedE2eDatabases) -> Engine:
    metadata.create_all(mssql_databases.app_engine)
    return mssql_databases.app_engine


@pytest.fixture
def connection(multiquery_engine: Engine) -> Iterator[Connection]:
    """Seed rows in a transaction that is rolled back after each test."""
    with multiquery_engine.connect() as connection, connection.begin() as transaction:
        connection.execute(
            parent.insert(),
            [{"id": 1, "name": "Parent"}, {"id": 3, "name": "Other"}],
        )
        connection.execute(child.insert(), {"id": 2, "parent_id": 1, "name": "Child"})
        yield connection
        transaction.rollback()


_SKIP_ROWCOUNTS = {"mssql_skip_rowcounts": True}


@pytest.fixture(params=[False, True], ids=["default", "opted-in"])
def skip_rowcounts(request: pytest.FixtureRequest) -> bool:
    return request.param


@pytest.fixture
def cursor_result(
    connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Result[*tuple[Any, ...]], _Cursor]]:
    with _doubled_result(connection, monkeypatch, {}) as doubled:
        yield doubled


@contextmanager
def _doubled_result(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, execution_options: dict[str, Any],
) -> Generator[tuple[Result[*tuple[Any, ...]], _Cursor]]:
    """Execute a real statement with the given options, then swap its cursor for a ``_Cursor``."""
    cursor = _Cursor((
        (("id", "name"), [(1, "Name"), (2, None)]),
        (None, []),
        (("empty",), []),
        (("total",), [(2,)]),
    ))
    with connection.execute(text("SELECT 1"), execution_options=execution_options) as result:
        assert isinstance(result, CursorResult)
        assert result.cursor is not None
        result.cursor.close()
        monkeypatch.setattr(result, "cursor", cursor)
        yield result, cursor


@pytest.fixture
def option_cursor_result(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, skip_rowcounts: bool,
) -> Iterator[tuple[Result[*tuple[Any, ...]], _Cursor]]:
    with _doubled_result(connection, monkeypatch, _SKIP_ROWCOUNTS if skip_rowcounts else {}) as doubled:
        yield doubled


def test_read_all_result_sets_reads_all_sets_and_closes_cursor(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
) -> None:
    result, cursor = cursor_result

    assert read_all_result_sets(result) == [
        [{"id": 1, "name": "Name"}, {"id": 2, "name": None}],
        [],
        [],
        [{"total": 2}],
    ]
    assert cursor.fetches == [0, 2, 3]
    assert result.closed
    assert cursor.closes == 1


@pytest.mark.parametrize("operation", ["fetchall", "nextset"])
def test_read_all_result_sets_keeps_non_driver_errors_and_closes_cursor(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    result, cursor = cursor_result
    error = RuntimeError(f"{operation} failed")

    def fail() -> None:
        raise error

    monkeypatch.setattr(cursor, operation, fail)
    with pytest.raises(RuntimeError) as caught:
        read_all_result_sets(result)

    assert caught.value is error
    assert result.closed
    assert cursor.closes == 1


def test_read_all_result_sets_rejects_non_cursor_results(connection: Connection) -> None:
    result = connection.execute(select(parent.c.id)).freeze()()

    with pytest.raises(TypeError, match="Expected a CursorResult"):
        read_all_result_sets(result)

    assert result.closed


def test_read_all_result_sets_rejects_missing_cursor(connection: Connection) -> None:
    result = connection.exec_driver_sql("UPDATE parent SET name = name")

    with pytest.raises(ValueError, match="no active cursor"):
        read_all_result_sets(result)

    assert result.closed


def test_read_all_result_sets_rejects_closed_cursor(connection: Connection) -> None:
    result = connection.exec_driver_sql("SELECT 1")
    result.close()

    with pytest.raises(ValueError, match="no active cursor"):
        read_all_result_sets(result)

    assert result.closed


@pytest.mark.parametrize(
    ("result_set", "expected"),
    [
        ((("id", "name"), [(1, None)]), [{"id": 1, "name": None}]),
        ((None, []), []),
        ((("id",), []), []),
    ],
)
def test_cursor_rows_to_dicts_does_not_advance_or_close(
    result_set: _CursorSet, expected: list[dict[str, object]],
) -> None:
    cursor = _Cursor((result_set,))

    assert cursor_rows_to_dicts(cursor) == expected
    assert cursor.index == 0
    assert not cursor.closed
    assert cursor.fetches == ([] if result_set[0] is None else [0])


def test_read_all_result_sets_closes_cursor_on_invalid_row_width(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
) -> None:
    result, cursor = cursor_result
    cursor.result_sets = ((("id", "name"), [(1,)]),)

    with pytest.raises(ValueError, match="zip"):
        read_all_result_sets(result)

    assert result.closed
    assert cursor.closes == 1


def _delayed_conversion_error() -> pyodbc.DataError:
    return pyodbc.DataError(
        "22018",
        "[22018] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Conversion failed "
        "when converting the varchar value 'x' to data type int. (245) (SQLMoreResults)",
    )


def test_read_all_result_sets_keeps_legacy_placeholders_for_non_row_results(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
) -> None:
    result, cursor = cursor_result
    cursor.result_sets = (
        (None, []),
        (("a",), [(1,)]),
        (None, []),
        (None, []),
        (("b",), [(2,)]),
        (None, []),
    )

    assert read_all_result_sets(result) == [[], [{"a": 1}], [], [], [{"b": 2}], []]
    assert cursor.fetches == [1, 4]
    assert result.closed


def test_read_all_result_sets_omits_non_row_results_but_keeps_empty_rowsets_with_the_option(
    connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _doubled_result(connection, monkeypatch, _SKIP_ROWCOUNTS) as (result, cursor):
        cursor.result_sets = (
            (("a",), [(1,)]),
            (None, []),
            (("empty",), []),
            (None, []),
            (("b",), [(2,)]),
            (None, []),
            (None, []),
        )

        assert read_all_result_sets(result) == [[{"a": 1}], [], [{"b": 2}]]

    # Trailing non-row results are still traversed, so their errors would be reported.
    assert cursor.index == len(cursor.result_sets) - 1
    assert cursor.fetches == [0, 2, 4]
    assert cursor.closes == 1


@pytest.mark.parametrize("operation", ["nextset", "fetchall", "trailing-nextset"])
def test_read_all_result_sets_adapts_driver_errors_after_a_valid_rowset(
    option_cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    result, cursor = option_cursor_result
    error = _delayed_conversion_error()
    if operation == "fetchall":
        cursor.result_sets = ((("a",), [(1,)]), (("b",), error))
    elif operation == "nextset":
        cursor.result_sets = ((("a",), [(1,)]), error)
    else:
        cursor.result_sets = ((("a",), [(1,)]), (None, []), error)

    with caplog.at_level(logging.ERROR), pytest.raises(DataError) as raised:
        read_all_result_sets(result)

    assert raised.value.orig is error
    assert raised.value.__cause__ is error
    assert parse_mssql_error(raised.value).error_type is SqlServerErrorType.DATA_CONVERSION
    assert result.closed
    assert cursor.closes == 1
    assert caplog.records == []


def test_read_all_result_sets_preserves_the_primary_error_when_close_also_fails(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    result, cursor = cursor_result
    error = RuntimeError("nextset failed")
    cursor.result_sets = ((("a",), [(1,)]), error)
    cursor.close_error = pyodbc.Error("HY000", "close failed")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as raised:
        read_all_result_sets(result)

    assert raised.value is error
    assert result.closed
    assert cursor.closes == 1
    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == [cursor.close_error]


def test_read_all_result_sets_logs_a_close_failure_after_a_complete_read(
    cursor_result: tuple[Result[*tuple[Any, ...]], _Cursor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    result, cursor = cursor_result
    close_error = pyodbc.Error("HY000", "close failed")
    cursor.result_sets = ((("a",), [(1,)]),)
    cursor.close_error = close_error

    with caplog.at_level(logging.ERROR):
        assert read_all_result_sets(result) == [[{"a": 1}]]

    assert result.closed
    assert cursor.closes == 1
    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == [close_error]


def test_read_all_result_sets_keeps_server_placeholders_without_the_option(
    connection: Connection,
) -> None:
    result = connection.execute(text(
        "SELECT id FROM parent WHERE id = 1; UPDATE parent SET name = name; "
        "SELECT id FROM parent WHERE 1 = 0; UPDATE parent SET name = name"
    ))

    assert read_all_result_sets(result) == [[{"id": 1}], [], [], []]


@pytest.mark.parametrize("leading_dml", [False, True], ids=["leading-select", "leading-dml"])
def test_read_all_result_sets_omits_only_non_row_results_with_the_option(
    connection: Connection, leading_dml: bool,
) -> None:
    statement = (
        "SELECT id FROM parent WHERE id = 1; UPDATE parent SET name = name; "
        "SELECT id FROM parent WHERE 1 = 0; UPDATE parent SET name = name"
    )
    if leading_dml:
        statement = f"UPDATE parent SET name = name; {statement}"
    result = connection.execute(text(statement), execution_options=_SKIP_ROWCOUNTS)

    assert read_all_result_sets(result) == [[{"id": 1}], []]


def test_read_all_result_sets_rejects_fully_traversed_dml_only_batches(connection: Connection) -> None:
    result = connection.execute(text("UPDATE parent SET name = name"), execution_options=_SKIP_ROWCOUNTS)

    with pytest.raises(ValueError, match="no active cursor"):
        read_all_result_sets(result)


@pytest.mark.parametrize(
    ("failing_statement", "error_type"),
    [
        ("SELECT CAST('x' AS int) AS b", SqlServerErrorType.DATA_CONVERSION),
        ("UPDATE parent SET name = name WHERE id = 1 / 0", SqlServerErrorType.DIVIDE_BY_ZERO),
        ("UPDATE parent SET name = name; UPDATE parent SET name = name WHERE id = 1 / 0",
         SqlServerErrorType.DIVIDE_BY_ZERO),
    ],
    ids=["delayed-select", "trailing-dml", "after-a-row-count"],
)
def test_read_all_result_sets_raises_server_errors_after_a_valid_rowset(
    connection: Connection,
    caplog: pytest.LogCaptureFixture,
    skip_rowcounts: bool,
    failing_statement: str,
    error_type: SqlServerErrorType,
) -> None:
    result = connection.execute(
        text(f"SELECT id FROM parent WHERE id = 1; {failing_statement}"),
        execution_options=_SKIP_ROWCOUNTS if skip_rowcounts else {},
    )

    with caplog.at_level(logging.ERROR), pytest.raises(DBAPIError) as raised:
        read_all_result_sets(result)

    assert isinstance(raised.value.orig, pyodbc.Error)
    assert raised.value.__cause__ is raised.value.orig
    assert parse_mssql_error(raised.value).error_type is error_type
    assert result.closed
    # The cursor is closed once; a second close would log "Error closing cursor".
    assert caplog.records == []
    # The transaction is left to its owner.
    assert connection.in_transaction()


@pytest.mark.parametrize("section", [renamed, _RenamedModel, _RenamedModel()])
def test_qualified_columns_use_database_names_and_split_rows_use_keys(
    connection: Connection, section: Table | type[DeclarativeBase] | DeclarativeBase,
) -> None:
    connection.execute(renamed.insert(), {"python_id": 1, "python_name": "Name"})
    columns = qualified_columns("r", section)
    row = connection.execute(text(f"SELECT {columns} FROM renamed AS r")).one()

    assert columns == "r.[db_id],\n            r.[db_name]"
    assert section_columns(section) == ("python_id", "python_name")
    assert split_row_sections(tuple(row), section) == (
        {"python_id": 1, "python_name": "Name"},
    )


def test_qualified_columns_preserve_tuple_names_and_order() -> None:
    assert qualified_columns("t", ("name", "id")) == "t.[name],\n            t.[id]"
    assert section_columns(("name", "id")) == ("name", "id")


@pytest.mark.parametrize(
    "section",
    [
        ("display]name",),
        Table("escaped", MetaData(), Column("display]name", String)),
    ],
)
def test_qualified_columns_escape_closing_brackets(
    section: tuple[str, ...] | Table,
) -> None:
    assert qualified_columns("t", section) == "t.[display]]name]"


@pytest.mark.parametrize("row_values", [(), (1,), (1, "Name"), (1, "Name", 2, "extra")])
def test_split_row_sections_rejects_width_mismatches(row_values: tuple[object, ...]) -> None:
    with pytest.raises(
        ValueError,
        match=f"Row width mismatch: expected 3 values, got {len(row_values)}",
    ):
        split_row_sections(row_values, ("id", "name"), ("other_id",))


def test_split_row_sections_preserves_null_sections_and_partial_nulls() -> None:
    assert split_row_sections(
        (1, None, None, None), ("id", "name"), ("other_id", "other_name")
    ) == ({"id": 1, "name": None}, None)


def test_split_row_sections_accepts_empty_rows_without_columns() -> None:
    assert split_row_sections(()) == ()
    assert split_row_sections((), ()) == (None,)
    assert split_row_sections((1,), (), ("id",)) == (None, {"id": 1})


def test_split_row_sections_rejects_values_without_sections() -> None:
    with pytest.raises(ValueError, match="Row width mismatch: expected 0 values, got 1"):
        split_row_sections((1,))


def test_map_row_to_pydantic_maps_root_columns_and_labeled_expressions(
    connection: Connection,
) -> None:
    row = connection.execute(
        select(
            parent.c.id,
            parent.c.name.label("display_name"),
            child,
        ).join(child, child.c.parent_id == parent.c.id)
    ).one()

    assert map_row_to_pydantic(row, _ParentDto, {"child": child}) == _ParentDto(
        id=1,
        display_name="Parent",
        child=_ChildDto(id=2, name="Child"),
    )


def test_map_row_to_pydantic_does_not_promote_nested_columns_to_root_fields(
    connection: Connection,
) -> None:
    row = connection.execute(select(child)).one()

    assert map_row_to_pydantic(row, _ChildOnlyDto, {"child": child}) == _ChildOnlyDto(
        child=_ChildDto(id=2, name="Child"),
    )


@pytest.mark.parametrize(
    "statement",
    [text("SELECT 7 AS total"), select(literal(7).label("total"))],
)
def test_map_row_to_pydantic_maps_root_only_rows(
    connection: Connection, statement: Executable,
) -> None:
    row = connection.execute(statement).one()

    assert map_row_to_pydantic(row, _RootDto, {}) == _RootDto(total=7)


@pytest.mark.parametrize("parent_id", [1, 3])
def test_map_row_to_pydantic_preserves_nested_fields_over_root_name_collisions(
    connection: Connection, parent_id: int,
) -> None:
    row = connection.execute(
        select(
            parent.c.id,
            parent.c.name.label("display_name"),
            child,
            literal(9).label("child"),
        )
        .outerjoin(child, child.c.parent_id == parent.c.id)
        .where(parent.c.id == parent_id)
    ).one()

    assert map_row_to_pydantic(row, _ParentDto, {"child": child}) == _ParentDto(
        id=parent_id,
        display_name="Parent" if parent_id == 1 else "Other",
        child=_ChildDto(id=2, name="Child") if parent_id == 1 else None,
    )


def test_map_row_to_pydantic_maps_text_with_explicit_column_metadata(
    connection: Connection,
) -> None:
    row = connection.execute(
        text(
            "SELECT parent.id, parent.name AS display_name, child.id, child.parent_id, child.name "
            "FROM parent JOIN child ON child.parent_id = parent.id"
        ).columns(parent.c.id, parent.c.name.label("display_name"), *child.c)
    ).one()

    assert map_row_to_pydantic(row, _ParentDto, {"child": child}) == _ParentDto(
        id=1,
        display_name="Parent",
        child=_ChildDto(id=2, name="Child"),
    )


def test_map_row_to_pydantic_maps_table_aliases(connection: Connection) -> None:
    child_alias = child.alias("joined_child")
    row = connection.execute(
        select(parent.c.id, parent.c.name.label("display_name"), child_alias)
        .join(child_alias, child_alias.c.parent_id == parent.c.id)
    ).one()

    assert map_row_to_pydantic(row, _ParentDto, {"child": child_alias}) == _ParentDto(
        id=1,
        display_name="Parent",
        child=_ChildDto(id=2, name="Child"),
    )


def test_map_row_to_pydantic_maps_required_nested_models(connection: Connection) -> None:
    row = connection.execute(select(child)).one()

    assert map_row_to_pydantic(row, _RequiredChildDto, {"child": child}) == _RequiredChildDto(
        child=_ChildDto(id=2, name="Child"),
    )


def test_map_row_to_pydantic_rejects_null_for_required_nested_model(
    connection: Connection,
) -> None:
    row = connection.execute(
        select(child)
        .select_from(parent.outerjoin(child, child.c.parent_id == parent.c.id))
        .where(parent.c.id == 3)
    ).one()

    with pytest.raises(ValidationError, match="child"):
        map_row_to_pydantic(row, _RequiredChildDto, {"child": child})


@pytest.mark.parametrize("target_model", [_AmbiguousDto, _OptionalAmbiguousDto])
@pytest.mark.parametrize("parent_id", [1, 3])
def test_map_row_to_pydantic_rejects_ambiguous_model_unions(
    connection: Connection, target_model: type[BaseModel], parent_id: int,
) -> None:
    row = connection.execute(
        select(child)
        .select_from(parent.outerjoin(child, child.c.parent_id == parent.c.id))
        .where(parent.c.id == parent_id)
    ).one()

    with pytest.raises(TypeError, match="Nested field 'child'.*multiple Pydantic models"):
        map_row_to_pydantic(row, target_model, {"child": child})


def test_map_row_to_pydantic_uses_physical_names_for_nested_validation_aliases(
    connection: Connection,
) -> None:
    connection.execute(renamed.insert(), {"python_id": 1, "python_name": "Name"})
    row = connection.execute(select(renamed)).one()

    mapped = map_row_to_pydantic(row, _NestedRenamedDto, {"record": renamed})

    assert mapped.model_dump() == {"record": {"python_id": 1, "python_name": "Name"}}


# Script batches (DB-IMPLEMENTATION.md, R5).
_LARGE_ROWSET = "SELECT TOP (100000) a.object_id FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b"


@dataclass
class _ScriptCursor(_Cursor):
    """A ``_Cursor`` that also executes and fetches in chunks; ``fetchall`` is never expected."""

    executed: list[str] = field(default_factory=list)
    fetch_sizes: list[int] = field(default_factory=list)
    execute_error: BaseException | None = None
    offset: int = 0

    def execute(self, statement: str) -> None:
        self.executed.append(statement)
        if self.execute_error is not None:
            raise self.execute_error

    def fetchall(self) -> list[tuple[object, ...]]:
        raise AssertionError("script batches must be fetched in bounded chunks")

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        self.fetch_sizes.append(size)
        rows = self._current()[1]
        if isinstance(rows, BaseException):
            raise rows
        chunk = rows[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def nextset(self) -> bool | None:
        self.offset = 0
        return super().nextset()


@dataclass
class _ScriptLog:
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def warning(self, event_name: str, /, **fields: object) -> None:
        self.events.append((event_name, fields))


@pytest.fixture
def script_log(monkeypatch: pytest.MonkeyPatch) -> _ScriptLog:
    log = _ScriptLog()
    monkeypatch.setattr(multiquery_module, "_logger", log)
    return log


def _use_script_cursor(connection: Connection, monkeypatch: pytest.MonkeyPatch, cursor: _ScriptCursor) -> None:
    monkeypatch.setattr(connection.connection, "cursor", lambda: cursor)


def _record_driver_cursors(connection: Connection, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    created: list[Any] = []
    fairy = connection.connection
    real_cursor = fairy.cursor

    def cursor() -> Any:
        created.append(real_cursor())
        return created[-1]

    monkeypatch.setattr(fairy, "cursor", cursor)
    return created


def test_execute_script_batch_drains_every_result_in_bounded_chunks(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, script_log: _ScriptLog,
) -> None:
    cursor = _ScriptCursor((
        (("a",), [(n,) for n in range(2500)]),
        (None, []),
        (("empty",), []),
        (("b",), [(1,)]),
        (None, []),
    ))
    _use_script_cursor(connection, monkeypatch, cursor)

    execute_script_batch(connection, "batch")

    assert cursor.executed == ["batch"]
    assert cursor.fetch_sizes == [1000] * 4 + [1000] + [1000] * 2
    assert cursor.index == len(cursor.result_sets) - 1
    assert cursor.closes == 1
    assert script_log.events == []
    assert connection.in_transaction()


@pytest.mark.parametrize("operation", ["execute", "fetchmany", "nextset", "trailing-nextset"])
def test_execute_script_batch_adapts_driver_errors_and_closes_the_cursor(
    connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
    script_log: _ScriptLog,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    error = _delayed_conversion_error()
    result_sets: dict[str, tuple[_CursorSet | BaseException, ...]] = {
        "execute": ((("a",), [(1,)]),),
        "fetchmany": ((("a",), [(1,)]), (("b",), error)),
        "nextset": ((("a",), [(1,)]), error),
        "trailing-nextset": ((("a",), [(1,)]), (None, []), error),
    }
    cursor = _ScriptCursor(result_sets[operation])
    if operation == "execute":
        cursor.execute_error = error
    _use_script_cursor(connection, monkeypatch, cursor)

    with caplog.at_level(logging.ERROR), pytest.raises(DataError) as raised:
        execute_script_batch(connection, "batch")

    assert raised.value.orig is error
    assert raised.value.__cause__ is error
    assert raised.value.statement == "batch"
    assert parse_mssql_error(raised.value).error_type is SqlServerErrorType.DATA_CONVERSION
    assert cursor.closes == 1
    assert script_log.events == []
    assert caplog.records == []
    # The transaction is left to its owner.
    assert connection.in_transaction()


def test_execute_script_batch_keeps_non_driver_errors(
    connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("fetch failed")
    cursor = _ScriptCursor((((("a",), error)),))
    _use_script_cursor(connection, monkeypatch, cursor)

    with pytest.raises(RuntimeError) as raised:
        execute_script_batch(connection, "batch")

    assert raised.value is error
    assert cursor.closes == 1


def test_execute_script_batch_raises_a_close_failure_after_a_complete_batch(
    connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = pyodbc.Error("HY000", "close failed")
    cursor = _ScriptCursor(((("a",), [(1,)]),), close_error=close_error)
    _use_script_cursor(connection, monkeypatch, cursor)

    with pytest.raises(DBAPIError) as raised:
        execute_script_batch(connection, "batch")

    assert raised.value.orig is close_error
    assert cursor.closes == 1


def test_execute_script_batch_preserves_the_primary_error_and_logs_a_close_failure(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, script_log: _ScriptLog,
) -> None:
    error = _delayed_conversion_error()
    close_error = pyodbc.Error("HY000", "close failed")
    cursor = _ScriptCursor(((("a",), [(1,)]), error), close_error=close_error)
    _use_script_cursor(connection, monkeypatch, cursor)

    with pytest.raises(DataError) as raised:
        execute_script_batch(connection, "batch")

    assert raised.value.orig is error
    assert cursor.closes == 1
    assert script_log.events == [("db.script.cursor_close_failed", {"exc_info": close_error})]


@pytest.mark.parametrize(("name", "driver"), [("postgresql", "psycopg"), ("mssql", "aioodbc")])
def test_execute_script_batch_rejects_unsupported_drivers_before_running(name: str, driver: str) -> None:
    unsupported = SimpleNamespace(dialect=SimpleNamespace(name=name, driver=driver))

    with pytest.raises(ArgumentError, match=f"not {name}\\+{driver}"):
        execute_script_batch(cast(Connection, unsupported), "SELECT 1")


@pytest.fixture(params=["pyodbc", "pymssql"])
def script_engine(request: pytest.FixtureRequest, multiquery_engine: Engine) -> Iterator[Engine]:
    if request.param == "pyodbc":
        yield multiquery_engine
        return
    engine = create_engine(multiquery_engine.url.set(drivername="mssql+pymssql", query={}))
    yield engine
    engine.dispose()


@pytest.fixture
def script_connection(script_engine: Engine) -> Iterator[Connection]:
    with script_engine.connect() as connection:
        yield connection
        connection.rollback()


def _assert_closed(cursor: Any) -> None:
    with pytest.raises(Exception, match="(?i)closed"):
        cursor.execute("SELECT 1")


def test_execute_script_batch_runs_writes_after_a_large_discarded_rowset(
    script_connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_connection.exec_driver_sql("CREATE TABLE #script_writes (id int NOT NULL)")
    cursors = _record_driver_cursors(script_connection, monkeypatch)

    execute_script_batch(
        script_connection,
        f"INSERT INTO #script_writes VALUES (1); {_LARGE_ROWSET}; INSERT INTO #script_writes VALUES (2)",
    )

    assert len(cursors) == 1
    _assert_closed(cursors[0])
    assert script_connection.exec_driver_sql(
        "SELECT id FROM #script_writes ORDER BY id"
    ).scalars().all() == [1, 2]


@pytest.mark.parametrize(
    ("failing_statement", "error_type"),
    [
        ("SELECT CAST('x' AS int) AS b", SqlServerErrorType.DATA_CONVERSION),
        ("UPDATE parent SET name = name WHERE id = 1 / 0", SqlServerErrorType.DIVIDE_BY_ZERO),
        ("UPDATE parent SET name = name; UPDATE parent SET name = name WHERE id = 1 / 0",
         SqlServerErrorType.DIVIDE_BY_ZERO),
    ],
    ids=["delayed-select", "trailing-dml", "after-a-row-count"],
)
def test_execute_script_batch_raises_server_errors_after_a_valid_result(
    script_connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
    failing_statement: str,
    error_type: SqlServerErrorType,
) -> None:
    batch = f"SELECT id FROM parent; {failing_statement}"
    cursors = _record_driver_cursors(script_connection, monkeypatch)

    with pytest.raises(DBAPIError) as raised:
        execute_script_batch(script_connection, batch)

    assert isinstance(raised.value.orig, script_connection.dialect.loaded_dbapi.Error)
    assert raised.value.__cause__ is raised.value.orig
    assert raised.value.statement == batch
    if script_connection.dialect.driver == "pyodbc":
        # parse_mssql_error does not parse pymssql arguments.
        assert parse_mssql_error(raised.value).error_type is error_type
    assert script_connection.in_transaction()
    assert len(cursors) == 1
    _assert_closed(cursors[0])


def test_execute_script_batch_uses_the_callers_transaction_without_committing(script_engine: Engine) -> None:
    with script_engine.connect() as connection:
        execute_script_batch(connection, "CREATE TABLE script_uncommitted (id int); SELECT 1 AS a")

        assert connection.in_transaction()
        connection.rollback()
        assert connection.exec_driver_sql("SELECT OBJECT_ID(N'script_uncommitted')").scalar() is None
