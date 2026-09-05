from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Column, Executable, Integer, MetaData, String, Table, create_engine, literal, select, text
from sqlalchemy.engine import Connection, CursorResult
from sqlalchemy.orm import DeclarativeBase

from grad_pylib.core.multiquery import (
    cursor_rows_to_dicts,
    map_row_to_pydantic,
    qualified_columns,
    read_all_result_sets,
    section_columns,
    split_row_sections,
)


metadata = MetaData()
parent = Table(
    "parent",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
)
child = Table(
    "child",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("parent_id", Integer, nullable=False),
    Column("name", String, nullable=False),
)
renamed = Table(
    "renamed",
    metadata,
    Column("db_id", Integer, key="python_id", primary_key=True),
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


type _CursorSet = tuple[tuple[str, ...] | None, list[tuple[object, ...]]]


@dataclass
class _Cursor:
    result_sets: tuple[_CursorSet, ...]
    index: int = 0
    closed: bool = False
    fetches: list[int] = field(default_factory=list)

    @property
    def description(self) -> tuple[tuple[str], ...] | None:
        names, _ = self.result_sets[self.index]
        return tuple((name,) for name in names) if names is not None else None

    def fetchall(self) -> list[tuple[object, ...]]:
        self.fetches.append(self.index)
        return self.result_sets[self.index][1]

    def nextset(self) -> bool | None:
        if self.index + 1 == len(self.result_sets):
            return None
        self.index += 1
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def connection() -> Iterator[Connection]:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            parent.insert(),
            [{"id": 1, "name": "Parent"}, {"id": 3, "name": "Other"}],
        )
        connection.execute(child.insert(), {"id": 2, "parent_id": 1, "name": "Child"})
        yield connection
    engine.dispose()


@pytest.fixture
def cursor_result(
    connection: Connection, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[CursorResult[Any], _Cursor]]:
    cursor = _Cursor((
        (("id", "name"), [(1, "Name"), (2, None)]),
        (None, []),
        (("empty",), []),
        (("total",), [(2,)]),
    ))
    with connection.exec_driver_sql("SELECT 1") as result:
        assert result.cursor is not None
        result.cursor.close()
        monkeypatch.setattr(result, "cursor", cursor)
        yield result, cursor


def test_read_all_result_sets_reads_all_sets_and_closes_cursor(
    cursor_result: tuple[CursorResult[Any], _Cursor],
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
    assert cursor.closed


@pytest.mark.parametrize("operation", ["fetchall", "nextset"])
def test_read_all_result_sets_closes_cursor_on_driver_errors(
    cursor_result: tuple[CursorResult[Any], _Cursor],
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
    assert cursor.closed


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
    cursor_result: tuple[CursorResult[Any], _Cursor],
) -> None:
    result, cursor = cursor_result
    cursor.result_sets = ((("id", "name"), [(1,)]),)

    with pytest.raises(ValueError, match="zip"):
        read_all_result_sets(result)

    assert result.closed
    assert cursor.closed


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
