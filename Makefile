.PHONY: build check compile dev-authz dev-rag dev-session dev-web format install-hooks lint provision-database reset-dev-data test typecheck web-install

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
	uv run python -m compileall -q rag session shared authz tests

build: web-install
	npm --prefix web run build

check: web-install lint typecheck test compile build

dev-authz:
	uv run uvicorn authz.main:app --host 127.0.0.1 --port 8012 --reload

dev-rag:
	uv run uvicorn rag.main:app --host 127.0.0.1 --port 8010 --reload

dev-session:
	uv run uvicorn session.main:app --host 127.0.0.1 --port 8011 --reload

dev-web: web-install
	npm --prefix web run dev

reset-dev-data:
	@test "$(APP_ENV)" = "development" || (echo "APP_ENV must be development" && exit 1)
	@test "$(CONFIRM_RESET_DEV_DATA)" = "yes" || (echo "Set CONFIRM_RESET_DEV_DATA=yes to confirm deletion" && exit 1)
	APP_ENV=$(APP_ENV) CONFIRM_RESET_DEV_DATA=$(CONFIRM_RESET_DEV_DATA) uv run python scripts/reset_dev_data.py

provision-database:
	@test -n "$(DATABASE_OWNER_URL)" || (echo "DATABASE_OWNER_URL is required" && exit 1)
	DATABASE_OWNER_URL=$(DATABASE_OWNER_URL) DATABASE_SCHEMA_PROVISIONING=yes uv run python scripts/provision_database.py

install-hooks:
	uv run pre-commit install
