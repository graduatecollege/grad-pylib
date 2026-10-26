from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import Engine

from grad_pylib.tools import rebuild_models as rebuild_module


def test_rebuild_models_configures_memory_before_provisioning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    admin_url = "mssql+pymssql://localhost/tempdb"
    container = MagicMock()
    container.__enter__.return_value = container
    container.get_connection_url.return_value = admin_url
    container_factory = MagicMock(return_value=container)
    monkeypatch.setattr(rebuild_module, "sqlserver_container_class", lambda: container_factory)
    configure_memory = MagicMock()
    monkeypatch.setattr(rebuild_module, "configure_sql_server_memory", configure_memory)
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr(rebuild_module, "create_engine", MagicMock(return_value=engine))
    migrate = MagicMock()
    generate = MagicMock()
    monkeypatch.setattr(rebuild_module, "run_migrations", migrate)
    monkeypatch.setattr(rebuild_module, "generate_models", generate)

    def create_database(url: str, name: str) -> str:
        configure_memory.assert_called_once_with(url)
        assert name == "App"
        return "app-url"

    monkeypatch.setattr(rebuild_module, "create_database", create_database)
    output = tmp_path / "models.py"

    assert rebuild_module.rebuild_models(str(output), schema_dir="schema") == output

    configure_memory.assert_called_once_with(admin_url)
    assert container_factory.call_args.kwargs["mem_limit"] == "2g"
    assert "env" not in container_factory.call_args.kwargs
    migrate.assert_called_once_with(engine, schema_dir="schema")
    generate.assert_called_once_with(output_path=str(output), database_url="app-url", bidirectional=False)
    engine.dispose.assert_called_once_with()
    container.__exit__.assert_called_once()
