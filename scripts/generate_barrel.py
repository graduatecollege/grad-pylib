# scripts/generate_barrel.py
import ast
import fnmatch
import os
from pathlib import Path

# Matches are evaluated against the path relative to src/grad_pylib/
EXCLUDE_PATTERNS = [
    "__init__.py",  # Always ignore init files
    "test_*.py",  # Any test files in the root
    "**/test_*.py",  # Any test files in subdirectories
    "tools/*.py",
    "testing/*.py",
    "testclient.py"
]


def should_exclude(file_path: Path, pkg_root: Path) -> bool:
    """Checks if a file path matches any gitignore-style pattern."""
    # Get the relative path string using forward slashes (Unix style) for consistent matching
    # e.g., "tests/test_db.py" or "auth/internal/helpers.py"
    relative_path = str(file_path.relative_to(pkg_root)).replace(os.sep, "/")

    for pattern in EXCLUDE_PATTERNS:
        # If the pattern contains a glob star or directory separator, match against the whole path
        if fnmatch.fnmatch(relative_path, pattern):
            return True
        # Fallback to match just the filename if it's a simple pattern (like test_*.py)
        if fnmatch.fnmatch(file_path.name, pattern):
            return True

    return False


def extract_public_definitions(file_path: Path):
    """Parses a python file and returns all public class and function names."""
    with open(file_path, encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=str(file_path))
        except SyntaxError:
            return []

    public_names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public_names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    public_names.append(target.id)
    return public_names


def build_barrel():
    pkg_root = Path("src/grad_pylib")
    import_lines = []
    all_exports = []

    # Recursively crawl ALL files and subdirectories
    for root, _, files in os.walk(pkg_root):
        for file in files:
            file_path = Path(root) / file

            # Check file extension and evaluate against our path matcher
            if file.endswith(".py") and not should_exclude(file_path, pkg_root):

                # Turn filesystem path into a python import path
                relative_parts = file_path.relative_to(pkg_root.parent).with_suffix("").parts
                module_path = ".".join(relative_parts)

                public_symbols = extract_public_definitions(file_path)

                if public_symbols:
                    symbols_str = ", ".join(public_symbols)
                    import_lines.append(f"from {module_path} import {symbols_str}")
                    all_exports.extend(public_symbols)

    # Write the completely hands-free root __init__.py
    init_file = pkg_root / "__init__.py"
    with open(init_file, "w", encoding="utf-8") as f:
        f.write("# 🚨 AUTO-GENERATED BARREL FILE. DO NOT EDIT MANUALLY.\n")
        f.write("# Run `pdm run barrel` to regenerate.\n\n")

        # Write the absolute imports
        f.write("\n".join(sorted(import_lines)))
        f.write("\n\n__all__ = [\n")

        # Write the __all__ block
        for export in sorted(all_exports):
            f.write(f'    "{export}",\n')
        f.write("]\n")

    print(f"✅ Successfully mapped {len(all_exports)} public helpers into the root barrel file!")


if __name__ == "__main__":
    build_barrel()