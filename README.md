[![CI](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml/badge.svg)](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml)

# Xian

ABCI application for running a Xian node on CometBFT 0.38.12.

## Development

This repo assumes sibling checkouts of:

- `../xian-contracting`
- `../xian-py`

Bootstrap the local environment with `uv`:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
```

Run the standard validation commands:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
```

Or use the repo helper that runs the same contract:

```bash
./scripts/validate-repo.sh
```

The test suite expects a local Postgres instance at `postgres://postgres:1234@localhost:5432/xian` for BDS-backed coverage.

## Runtime

Operator setup and container orchestration live in [xian-stack](https://github.com/xian-technology/xian-stack). This repo should stay focused on deterministic node behavior, reusable setup helpers, and ABCI-facing functionality.

The bundled files under `src/xian/tools/genesis/` are legacy network assets. They remain here temporarily for compatibility and are slated to move out so this repo becomes a universal Xian node runtime.
