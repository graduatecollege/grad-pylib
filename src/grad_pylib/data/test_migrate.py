from pathlib import Path

from sqlalchemy import create_engine, text

from grad_pylib.data.migrate import run_migrations, split_batches


def test_split_batches_supports_go_delimiter() -> None:
    batches = split_batches("create table a(id int);\ngo\ncreate table b(id int);")
    assert batches == ["create table a(id int);", "create table b(id int);"]


def test_run_migrations_uses_schema_dir_parameter(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "001-create.sql").write_text("create table demo(id integer);", encoding="utf-8")

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        run_migrations(engine, schema_dir=schema_dir)
        with engine.connect() as conn:
            conn.execute(text("select * from demo"))
    finally:
        engine.dispose()
