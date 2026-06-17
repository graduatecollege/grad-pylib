.PHONY: checks barrel

checks:
	uv run ruff check .
	uv run ty check .
	uv run pytest -q

barrel:
	uv run python scripts/generate_barrel.py