[![CI](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml/badge.svg)](https://github.com/xian-technology/xian-abci/actions/workflows/validate.yml)

# xian-abci

`xian-abci` is the CometBFT-facing Xian node application. It owns deterministic
chain behavior, ABCI request handling, node setup primitives, state export and
restore flows, and node-facing services such as BDS and the optional dashboard.

## Scope

This repo owns:

- ABCI methods and block-processing logic
- node setup, node admin, genesis building, and state snapshot helpers
- in-process transaction simulation, rewards, validator handling, and metrics
- optional node-adjacent services such as BDS and the dashboard

This repo does not own:

- end-user operator UX such as `network join` or `node start`
- Docker or Compose topology
- canonical network manifests and chain asset bundles

## Key Directories

- `src/xian/methods/`: ABCI request handlers and query surfaces
- `src/xian/`: node services, setup helpers, state export/sync, and shared utilities
- `src/xian/cli/`: backend/operator entrypoints owned by this repo
- `scripts/`: repo validation and protobuf generation helpers
- `tests/`: unit, integration, and system coverage for node behavior

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
./scripts/validate-repo.sh
```

The BDS-backed test paths expect Postgres at
`postgres://postgres:1234@localhost:5432/xian`.

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/README.md](docs/README.md)
- [docs/LEGACY_CHAIN_ASSETS.md](docs/LEGACY_CHAIN_ASSETS.md)

## Runtime Surface

`xian-abci` exposes single-process entrypoints. It does not own a bundled
process manager:

```bash
uv run xian-abci
uv run xian-dashboard --rpc-url http://127.0.0.1:26657
uv run xian-configure-node --help
uv run xian-export-state --help
cometbft node --rpc.laddr tcp://0.0.0.0:26657
```

If you need supervision, use `xian-stack`, Docker, `systemd`, or `launchd`.

## Boundary Notes

- Operator flows belong in `xian-cli`.
- Runtime/backend orchestration belongs in `xian-stack`.
- Canonical chain fixtures and manifests belong in `xian-configs`.
- Reusable bootstrap logic should live in importable helpers such as
  `src/xian/genesis_builder.py`, `src/xian/node_admin.py`, and
  `src/xian/state_export.py`, not in ad hoc scripts.
