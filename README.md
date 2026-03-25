# xian-abci

`xian-abci` is the CometBFT-facing Xian node application. It owns deterministic
chain behavior, ABCI request handling, state export and restore flows, and
node-adjacent services such as BDS and the optional dashboard.

## Quick Start

Run the ABCI application:

```bash
uv run xian-abci
```

Run the optional dashboard against a local CometBFT RPC endpoint:

```bash
uv run xian-dashboard --rpc-url http://127.0.0.1:26657
```

Inspect the backend-oriented CLI surface:

```bash
uv run xian-configure-node --help
uv run xian-export-state --help
uv run xian-state-snapshot --help
```

## Principles

- `xian-abci` owns deterministic node behavior and backend primitives, not the
  full operator UX.
- Process supervision, Docker topology, and operator workflows belong in
  `xian-stack`, `xian-cli`, or external tooling such as `systemd`.
- BDS, metrics, snapshots, and the dashboard are part of the node-adjacent
  runtime surface, but the core ABCI path should remain understandable without
  them.
- Consensus-sensitive behavior belongs in reusable code, not ad hoc scripts or
  one-off operator commands.

## How It Fits

- use `xian-abci` when you need the actual node application or backend-oriented
  node tooling
- use `xian-cli` when you want the operator-facing UX around manifests,
  profiles, health, and recovery flows
- use `xian-stack` when you want the local Docker/Compose runtime and smoke
  flows
- use `xian-deploy` when you want remote Linux host deployment

## Key Directories

- `src/xian/methods/`: ABCI request handlers and query surfaces
- `src/xian/services/`: BDS, dashboard, metrics, state sync, and related services
- `src/xian/cli/`: backend-oriented command entrypoints owned by this repo
- `src/xian/`: node setup helpers, state export/import, and shared runtime code
- `scripts/`: repo validation and protobuf generation helpers
- `tests/`: unit, integration, governance, and system coverage

## What It Covers

- deterministic transaction processing and block finalization
- node queries and simulation
- state export, state snapshots, and state sync helpers
- node setup, home configuration, and genesis-building primitives
- BDS indexing and optional dashboard services
- backend-oriented CLI entrypoints such as:
  - `xian-configure-node`
  - `xian-export-state`
  - `xian-state-snapshot`
  - `xian-bds-reindex`
  - `xian-bds-snapshot`
  - `xian-bds-spool`

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
