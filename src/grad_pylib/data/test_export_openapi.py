from pathlib import Path

import pytest
from fastapi import FastAPI

from grad_pylib.data.export_openapi import (
    _resolve_app_factory,
    default_openapi_output_path,
    export_openapi,
)


def test_export_openapi_writes_document(tmp_path: Path) -> None:
    def app_factory() -> FastAPI:
        app = FastAPI(title="Test API")

        @app.get("/ping")
        def ping() -> dict[str, str]:
            return {"status": "ok"}

        return app

    output = tmp_path / "spec" / "openapi.json"
    path = export_openapi(app_factory, str(output), module_prefixes=("missing_prefix",))

    assert path == output
    assert output.exists()
    assert '"openapi"' in output.read_text(encoding="utf-8")


def test_default_openapi_output_path_uses_project_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "conference-awards-server"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert default_openapi_output_path() == Path("spec/conferenceawardsserver.json")


def test_default_openapi_output_path_prefers_openapi_ts_config(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "openapi-ts.config.ts").write_text(
        """
export default {
  input: 'confawardsserver.json',
};
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert default_openapi_output_path() == Path("spec/confawardsserver.json")


def test_resolve_app_factory_requires_module_callable_format() -> None:
    with pytest.raises(RuntimeError, match="App factory must use format"):
        _resolve_app_factory("invalid")
