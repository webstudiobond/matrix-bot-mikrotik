.PHONY: check install lint test verify

install:
	uv sync --locked

lint:
	uv run --frozen ruff format --check .
	uv run --frozen ruff check .

check:
	uv run --frozen ruff format --check .
	uv run --frozen ruff check .
	uv run --frozen mypy .
	uv run --frozen basedpyright .

test:
	uv run --frozen pytest

verify: check test
