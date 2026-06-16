import argparse
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

from sqlalchemy.engine import create_engine
from sqlalchemy.schema import MetaData

from grad_pylib.core.db import resolve_database_url

_GENERATOR_OPTIONS = {"use_inflect", "nojoined", "nobidi"}
_DEFAULT_IGNORED_TABLES = {"schema_migrations"}
_DEFAULT_STRING_COLLATION = "SQL_Latin1_General_CP1_CI_AS"


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


def generate_models(
        output_path: str | None = None,
        database_url: str | None = None,
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

    engine = create_engine(effective_database_url)
    try:
        metadata = MetaData()
        generator = generator_class(metadata, engine, _GENERATOR_OPTIONS)
        metadata.reflect(engine, None, generator.views_supported, should_reflect_table(effective_ignored_tables))
        normalize_default_collations(metadata, default_string_collation)
        target_path.write_text(generator.generate(), encoding="utf-8")
    finally:
        engine.dispose()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate SQLAlchemy models from a database.")
    parser.add_argument("--output-path", help="Path for generated models output.")
    parser.add_argument("--database-url", help="Database URL override.")
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    generate_models(output_path=args.output_path, database_url=args.database_url)
