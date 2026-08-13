.PHONY: lint format typecheck test compile check install-hooks

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run basedpyright

test:
	uv run pytest -q

compile:
	uv run python -m compileall -q app tests

check: lint typecheck test compile

install-hooks:
	uv run pre-commit install
