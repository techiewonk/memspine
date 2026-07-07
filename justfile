# memspine dev commands — run `just <recipe>`
set shell := ["bash", "-cu"]

# install dev environment with all extras
setup:
    uv sync --all-extras

# run the test suite
test:
    uv run pytest

# lint + format-check + typecheck
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy

# auto-fix formatting and lint
fix:
    uv run ruff format .
    uv run ruff check --fix .

# pre-commit gate: lint then test
check: lint test

# serve docs locally
docs:
    uv run mkdocs serve

# run the combination-matrix boot tests
combos:
    uv run pytest tests/combinations -q

# show the effective config for a template (e.g. just resolve personal)
resolve template="base":
    uv run memspine config resolve --template {{template}}
