# Repository Guidelines

## Scope
- `xian-abci` owns deterministic node behavior, CometBFT-facing ABCI methods, config/genesis primitives, and state handling.
- This repo is not the operator UX surface. New node lifecycle commands belong in `xian-cli`.
- Long term, this repo should be a universal Xian node runtime, not a home for network-specific genesis data.

## Project Layout
- `src/xian/methods/`: ABCI request handlers.
- `src/xian/services/`: background services such as simulator and BDS support.
- `src/xian/cli/`: backend and developer entrypoints that wrap importable
  helpers without living in legacy `tools/`.
- `src/xian/node_setup.py`: reusable CometBFT home and config helpers.
- `src/xian/node_admin.py`: importable helpers for configuring an initialized
  CometBFT home and applying snapshots.
- `src/xian/genesis_builder.py`: importable genesis-building helpers.
- `src/xian/state_export.py`: importable state-export helpers.
- `src/xian/tools/`: state-patch and one-off upgrade data, not the main command
  surface.
- `tests/`: unit, integration, system, governance, tools, and ABCI-method coverage.

## Change Routing
- Prefer extracting importable helpers from `src/xian/tools/` instead of adding more script-only logic.
- Keep backend entrypoints in `src/xian/cli/` if they still need a command
  surface. Do not route new behavior through `src/xian/tools/`.
- Do not add network-specific genesis files, seeds, or snapshots here. Those
  assets belong in the sibling `xian-configs` repo.
- Treat `xian-configs` as the source of truth for committed legacy chain
  fixtures. See `docs/LEGACY_CHAIN_ASSETS.md`.
- Changes to contract execution semantics usually belong in `xian-contracting`, not here.
- Container lifecycle or Compose changes belong in `xian-stack`, even when they are needed to run this repo.

## Validation
- Bootstrap: `UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev`
- Preferred full validation path: `./scripts/validate-repo.sh`
- Lint: `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
- Format check: `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .`
- Targeted node-setup tests: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/unit/test_node_setup.py`
- Full suite: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`
- Container/runtime smoke path, when relevant: `make init`, `make up`, `make down`

## Notes
- Local `uv` development depends on sibling checkouts of `../xian-contracting` and `../xian-py`.
- If you touch genesis or config rendering, keep `xian-cli` integration in mind and avoid baking in network-specific assumptions.
- Treat `src/xian/tools/` as a transition area. Prefer new importable code under `src/xian/` when possible.
