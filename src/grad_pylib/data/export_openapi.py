import argparse
import importlib
import inspect
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

DEFAULT_APP_FACTORY = "app:create_app"


def default_openapi_output_path() -> Path:
    config_path = Path.cwd() / "spec" / "openapi-ts.config.ts"
    if config_path.exists():
        config_text = config_path.read_text(encoding="utf-8")
        input_match = re.search(r"""input:\s*['"]([^'"]+\.json)['"]""", config_text)
        if input_match:
            return Path("spec") / input_match.group(1)

    pyproject_path = Path.cwd() / "pyproject.toml"
    project_name = "openapi"
    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project")
        if isinstance(project, dict):
            configured_name = project.get("name")
            if isinstance(configured_name, str) and configured_name.strip():
                project_name = configured_name

    sanitized_name = re.sub(r"[^A-Za-z0-9]+", "", project_name).lower()
    if not sanitized_name:
        sanitized_name = "openapi"
    return Path("spec") / f"{sanitized_name}.json"


def _resolve_app_factory(import_path: str) -> Callable[[], FastAPI]:
    module_name, separator, factory_name = import_path.partition(":")
    if not separator or not module_name or not factory_name:
        raise RuntimeError("App factory must use format '<module>:<callable>' (example: app:create_app).")

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Could not import module '{module_name}'. Ensure it is available from the current project."
        ) from exc

    factory = getattr(module, factory_name, None)
    if factory is None or not callable(factory):
        raise RuntimeError(
            f"Could not resolve callable '{factory_name}' in module '{module_name}'."
        )
    return factory


def extract_all_inline_docstrings(module_prefixes: Sequence[str] = ("features",)) -> dict[str, dict[str, str]]:
    docstring_registry: dict[str, dict[str, str]] = {}
    processed_classes: set[type[BaseModel]] = set()

    for module_name, module in list(sys.modules.items()):
        if not any(module_name.startswith(prefix) for prefix in module_prefixes):
            continue

        try:
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if not issubclass(cls, BaseModel) or cls is BaseModel or cls in processed_classes:
                    continue
                processed_classes.add(cls)

                source = inspect.getsource(cls)
                tokens = tokenize.tokenize(BytesIO(source.encode("utf-8")).readline)

                last_field: str | None = None
                class_descriptions: dict[str, str] = {}

                for token in tokens:
                    if token.type == tokenize.NAME and token.string in cls.model_fields:
                        last_field = token.string
                    elif token.type == tokenize.STRING and last_field:
                        docstring = token.string.strip('"\' \n\t')
                        if docstring:
                            class_descriptions[last_field] = docstring
                        last_field = None

                if class_descriptions:
                    docstring_registry[cls.__name__] = class_descriptions

        except Exception:
            continue

    return docstring_registry


def export_openapi(
        app_factory: Callable[[], FastAPI],
        output_path: str,
        *,
        module_prefixes: Sequence[str] = ("features",),
        post_export_command: Sequence[str] | None = None,
        post_export_cwd: str | None = None,
) -> Path:
    inline_docs = extract_all_inline_docstrings(module_prefixes)
    app = app_factory()
    document = app.openapi()
    components = document.get("components", {}).get("schemas", {})

    for model_name, model_schema in components.items():
        if "title" in model_schema:
            del model_schema["title"]

        properties = model_schema.get("properties", {})
        fields_docs = inline_docs.get(model_name, {})

        for field_name, property_schema in properties.items():
            if "title" in property_schema:
                del property_schema["title"]

            if field_name in fields_docs:
                description_text = fields_docs[field_name]
                property_schema["description"] = description_text.strip().replace("\n", " ")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    if post_export_command:
        subprocess.run(list(post_export_command), cwd=post_export_cwd, check=True)

    return path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export OpenAPI spec using project conventions.")
    parser.add_argument(
        "--output-path",
        default=str(default_openapi_output_path()),
        help="Path for generated OpenAPI JSON.",
    )
    parser.add_argument(
        "--app-factory",
        default=DEFAULT_APP_FACTORY,
        help="App factory import path in '<module>:<callable>' format.",
    )
    parser.add_argument(
        "--module-prefix",
        action="append",
        dest="module_prefixes",
        help="Module prefix to scan for inline field docstrings (repeatable).",
    )
    parser.add_argument(
        "--skip-post-export-build",
        action="store_true",
        help="Skip `npm run build` in the output directory after export.",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    output_path = Path(args.output_path)
    module_prefixes = tuple(args.module_prefixes) if args.module_prefixes else ("features",)
    post_export_command = None if args.skip_post_export_build else ["npm", "run", "build"]
    exported_path = export_openapi(
        _resolve_app_factory(args.app_factory),
        output_path=str(output_path),
        module_prefixes=module_prefixes,
        post_export_command=post_export_command,
        post_export_cwd=str(output_path.parent),
    )
    print(f"OpenAPI spec exported to {exported_path}")
