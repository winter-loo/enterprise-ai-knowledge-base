.PHONY: build check compile dev-rag dev-session dev-web format install-hooks lint test typecheck web-install

WEB_DEPENDENCY_STATE := web/node_modules/.package-lock.json

$(WEB_DEPENDENCY_STATE): web/package.json web/package-lock.json
	npm ci --prefix web

web-install: $(WEB_DEPENDENCY_STATE)

lint: web-install
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix web run lint

format: web-install
	uv run ruff check --fix .
	uv run ruff format .
	npm --prefix web run format

typecheck: web-install
	uv run basedpyright
	npm --prefix web run check

test: web-install
	uv run pytest -q
	npm --prefix web test

compile:
	uv run python -m compileall -q rag session tests

build: web-install
	npm --prefix web run build

check: web-install lint typecheck test compile build

dev-rag:
	uv run uvicorn rag.main:app --host 127.0.0.1 --port 8010 --reload

dev-session:
	uv run uvicorn session.main:app --host 127.0.0.1 --port 8011 --reload

dev-web: web-install
	npm --prefix web run dev

install-hooks:
	uv run pre-commit install
