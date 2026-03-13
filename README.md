[![CI](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml/badge.svg)](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml)

# xian-abci

`xian-abci` is the CometBFT-facing Xian node application. It owns deterministic
chain behavior, ABCI request handling, node setup primitives, and state
management. It does not own the operator CLI or the Docker/Compose runtime
surface.

## Ownership

This repo owns:

- ABCI methods and node processing under `src/xian/methods/`
- reusable node setup, config, and genesis helpers under `src/xian/`
- state utilities, rewards logic, validators, and BDS-facing services

This repo does not own:

- end-user node lifecycle commands such as `network join` or `node start`
- Compose topology, container startup, or local stack orchestration
- long-term canonical storage of network-specific genesis assets

## Development

The preferred workspace layout uses sibling checkouts of:

- `../xian-contracting`
- `../xian-py`

Bootstrap and validate with:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
./scripts/validate-repo.sh
```

Equivalent direct commands:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
```

The BDS-backed test paths expect Postgres at
`postgres://postgres:1234@localhost:5432/xian`.

## Boundary Notes

- Operator flows belong in [xian-cli](https://github.com/xian-technology/xian-cli).
- Runtime/backend orchestration belongs in [xian-stack](https://github.com/xian-technology/xian-stack).
- Existing files under `src/xian/tools/genesis/` are legacy compatibility assets
  and are slated to move out so this repo becomes a universal Xian node runtime.
- New genesis-building logic should live in importable helpers such as
  `src/xian/genesis_builder.py`, not as standalone scripts.
