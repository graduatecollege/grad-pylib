from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select

from grad_pylib.core.multiquery import map_row_to_pydantic


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


def test_map_row_to_pydantic_maps_root_columns_and_labeled_expressions() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(parent.insert(), {"id": 1, "name": "Parent"})
        connection.execute(child.insert(), {"id": 2, "parent_id": 1, "name": "Child"})
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


def test_map_row_to_pydantic_does_not_promote_nested_columns_to_root_fields() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(child.insert(), {"id": 2, "parent_id": 1, "name": "Child"})
        row = connection.execute(select(child)).one()

    assert map_row_to_pydantic(row, _ChildOnlyDto, {"child": child}) == _ChildOnlyDto(
        child=_ChildDto(id=2, name="Child"),
    )
