# Development Guide

This guide describes the complete local development workflow after cloning the
repository. Run all commands from the repository root.

## Prerequisites

Install the following tools on the development machine:

- Python 3.14 or newer
- `uv`
- GNU Make
- Docker Engine and Docker Compose, if you need to build or run containers
- `direnv`, optionally, for automatic virtual-environment activation

`uv` is the only Python-specific tool required to create the environment and
install dependencies. Dependency constraints are declared in `pyproject.toml`,
and exact resolved versions are recorded in `uv.lock`.

## Initial setup after cloning

```bash
git clone https://github.com/underhax/matrix-bot-mikrotik.git
cd matrix-bot-mikrotik
make install
```

`make install` runs `uv sync --locked`. It creates the root `.venv` and
installs the locked runtime and development dependencies. The command fails if
`uv.lock` is missing or out of date. In that case, run `uv lock`, review the
lockfile change, and run `make install` again.

## Using the virtual environment

The Makefile uses `uv run`, so activating `.venv` is not required for the
provided checks and tests.

For direct commands such as `python`, `pytest`, or `ruff`, use one of the
following options.

### Automatic activation with direnv

Create a local `.envrc` file in the repository root:

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
source .venv/bin/activate
```

`UV_CACHE_DIR` keeps the `uv` cache inside the project instead of using the
user-wide cache. The `.uv-cache/` directory is excluded by `.gitignore`.

Allow it once:

```bash
direnv allow
```

The `.envrc` file contains local shell configuration and must not be committed.

### Manual activation

Without `direnv`, activate the environment in each new shell:

```bash
source .venv/bin/activate
```

## Formatting, linting, type checks, and tests

Run the complete local verification:

```bash
make verify
```

Or run the stages separately:

```bash
make check
make test
```

`make check` runs Ruff formatting checks, Ruff linting, mypy, and BasedPyright.
`make test` runs the pytest suite.

These commands use the same locked dependencies as CI.

## Docker development image

The Docker build uses the repository root as its context and requires the root
`pyproject.toml` and `uv.lock` files.

```bash
# amd64 (default)
docker compose -f docker-compose.dev.yaml build

# arm64 (for example, Apple Silicon or Raspberry Pi)
TARGETARCH=arm64 docker compose -f docker-compose.dev.yaml build

# Force a full rebuild after a base-image update
docker compose -f docker-compose.dev.yaml build --no-cache
```

The Dockerfile installs only runtime dependencies with
`uv sync --frozen --no-dev`. It never modifies `uv.lock` during the image build.

## Updating dependencies

Edit dependency declarations only in `pyproject.toml`, then regenerate the
lockfile and verify the project:

```bash
uv lock
make install
make verify
```
