import argparse
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import Integer
from sqlalchemy import Column
from sqlalchemy.engine import create_engine
from sqlalchemy.schema import MetaData, PrimaryKeyConstraint
from sqlacodegen.models import ModelClass, RelationshipAttribute, RelationshipType

from grad_pylib.core.db import resolve_database_url

_GENERATOR_OPTIONS = {"use_inflect", "nojoined"}
_DEFAULT_IGNORED_TABLES = {"schema_migrations"}
_DEFAULT_STRING_COLLATION = "SQL_Latin1_General_CP1_CI_AS"


class _ColumnRenderer(Protocol):
    def render_column(self, column: object, show_name: bool, is_table: bool = False) -> str:
        ...


class _RelationshipArgumentRenderer(Protocol):
    def render_relationship_arguments(self, relationship: RelationshipAttribute) -> dict[str, object]:
        ...


class _RelationshipGenerator(Protocol):
    models: list[object]

    def generate_models(self) -> list[object]:
        ...


def default_generated_models_path() -> Path:
    return Path.cwd() / "src" / "data" / "generated" / "models.py"


def should_reflect_table(ignored_tables: set[str]) -> Callable[[str, MetaData], bool]:
    def include(table_name: str, _metadata: MetaData) -> bool:
        return table_name not in ignored_tables

    return include


def normalize_default_collations(metadata: MetaData, default_string_collation: str) -> None:
    tables = getattr(metadata, "tables", {})
    for table in tables.values():
        for column in table.columns:
            collation = getattr(column.type, "collation", None)
            if collation == default_string_collation:
                column.type.collation = None


def should_render_non_autoincrement_primary_key(column: object) -> bool:
    typed_column = cast(Column[object], column)
    primary_key = getattr(typed_column.table, "primary_key", None)
    identity = getattr(typed_column, "identity", None)
    autoincrement = getattr(typed_column, "autoincrement", None)
    column_type = getattr(typed_column, "type", None)

    return bool(
        typed_column.primary_key
        and isinstance(primary_key, PrimaryKeyConstraint)
        and len(primary_key.columns) == 1
        and isinstance(column_type, Integer)
        and identity is None
        and autoincrement is not True
    )


def relationship_local_constraint_columns(relationship: RelationshipAttribute) -> frozenset[Column[object]]:
    if relationship.constraint is None:
        return frozenset()

    return frozenset(
        column
        for column in relationship.constraint.columns
        if getattr(column, "table", None) is relationship.source.table
    )


def mark_secondary_overlapping_relationships_viewonly(models: list[object]) -> None:
    for model in models:
        if not isinstance(model, ModelClass):
            continue

        candidates: list[tuple[RelationshipAttribute, frozenset[Column[object]]]] = []
        for relationship in model.relationships:
            if relationship.type not in {RelationshipType.MANY_TO_ONE, RelationshipType.ONE_TO_ONE}:
                continue

            local_columns = relationship_local_constraint_columns(relationship)
            if local_columns:
                candidates.append((relationship, local_columns))

        kept_writable: list[frozenset[Column[object]]] = []
        for relationship, local_columns in sorted(
            candidates,
            key=lambda item: (
                -len(item[1]),
                item[0].target.table.fullname,
                item[0].name,
            ),
        ):
            if any(local_columns & writable_columns for writable_columns in kept_writable):
                setattr(relationship, "viewonly", True)
                continue

            kept_writable.append(local_columns)


def generator_with_non_autoincrement_primary_keys(generator_class: type) -> type:
    class GeneratorWithNonAutoincrementPrimaryKeys(generator_class):
        def generate_models(self) -> list[object]:
            models = cast(_RelationshipGenerator, cast(Any, super())).generate_models()
            mark_secondary_overlapping_relationships_viewonly(
                models
            )
            return models

        def render_column(self, column: object, show_name: bool, is_table: bool = False) -> str:
            rendered = cast(_ColumnRenderer, cast(object, super())).render_column(column, show_name, is_table)
            if should_render_non_autoincrement_primary_key(column):
                return f"{rendered[:-1]}, autoincrement=False)"

            return rendered

        def render_relationship_arguments(self, relationship: RelationshipAttribute) -> dict[str, object]:
            kwargs = dict(
                cast(_RelationshipArgumentRenderer, cast(object, super())).render_relationship_arguments(
                    relationship
                )
            )
            if getattr(relationship, "viewonly", False):
                kwargs["viewonly"] = True

            return kwargs

    return GeneratorWithNonAutoincrementPrimaryKeys


def generate_models(
        output_path: str | None = None,
        database_url: str | None = None,
        bidirectional: bool = False,
        *,
        ignored_tables: set[str] | None = None,
        default_string_collation: str = _DEFAULT_STRING_COLLATION,
) -> None:
    target_path = Path(output_path) if output_path else default_generated_models_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    effective_database_url = database_url or resolve_database_url()
    effective_ignored_tables = ignored_tables or _DEFAULT_IGNORED_TABLES

    generators = entry_points(group="sqlacodegen.generators")
    generator_class = next(ep for ep in generators if ep.name == "declarative").load()
    generator_class = generator_with_non_autoincrement_primary_keys(generator_class)

    opts = _GENERATOR_OPTIONS.copy()
    if not bidirectional:
        opts.add("nobidi")
        print("bidirectional=False")
    else:
        print("bidirectional=True")

    engine = create_engine(effective_database_url)
    try:
        metadata = MetaData()
        generator = generator_class(metadata, engine, opts)
        metadata.reflect(engine, None, generator.views_supported, should_reflect_table(effective_ignored_tables))
        normalize_default_collations(metadata, default_string_collation)
        target_path.write_text(generator.generate(), encoding="utf-8")
    finally:
        engine.dispose()



def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SQLAlchemy models from a database.")
    parser.add_argument("--output-path", help="Path for generated models output.")
    parser.add_argument("--database-url", help="Database URL override.")
    parser.add_argument("--bidirectional", help="Generate bidirectional relationships.", action="store_true")
    args = parser.parse_args()
    generate_models(output_path=args.output_path, database_url=args.database_url, bidirectional=args.bidirectional)
