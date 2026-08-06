.PHONY: checks barrel

lint:
	uv run ruff check .

barrel:
	uv run python scripts/generate_barrel.py

test:
	uv run pytest
