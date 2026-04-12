# xian-abci

`xian-abci` is the CometBFT-facing Xian node application. It owns deterministic
chain behavior, ABCI request handling, state export and restore flows, and
node-adjacent services such as BDS and the optional dashboard.

The published PyPI package name is `xian-tech-abci`. The console entrypoints
remain `xian-abci`, `xian-dashboard`, and the other `xian-*` commands exposed
by this repo.

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

Optional runtime extras:

- `uv sync --extra native` for the native admission/tracer helpers
- `uv sync --extra vm` for the experimental `xian_vm_v1` rollout path with:
  - explicit `authority=python` shadow mode
  - explicit `authority=native` native execution mode
  - stored-IR-first native preflight plus native execution wiring on explicit
    simulation requests and on the real tx path

The current `xian_vm_v1` rollout model is intentionally strict:

- `authority=python` means Python is authoritative and the native VM runs
  alongside it for comparison only
- `authority=native` means the native VM is authoritative for execution and
  chi/metering, and Python comparison is optional rather than mandatory
- native contract deployment is artifact-driven:
  `submission.submit_contract(...)` calls that must succeed under
  `authority=native` need `deployment_artifacts` instead of relying on a
  source-only compile path
- native deployment is also deterministic-context-driven:
  the native deploy path requires explicit `now`/block context from the node
  runtime and will not fall back to local wall-clock time
- `xian_vm_v1` execution is strict about artifacts:
  contracts must already carry persisted `__xian_ir_v1__`; stored
  `__source__` remains available for inspection, but it is not used as a
  runtime fallback
- `xian_vm_v1` shadow-mode rollout also enforces artifact-backed deployment:
  `submission.submit_contract(...)` with source only is rejected instead of
  being admitted and skipped by native comparison
- the node does not silently try native first and then hide problems behind a
  fallback to Python
- transaction simulation remains explicit client-triggered behavior; the node
  does not auto-run simulation for every incoming transaction

The BDS-backed test paths expect Postgres at
`postgres://postgres:1234@localhost:5432/xian`.

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/README.md](docs/README.md)
- [docs/LEGACY_CHAIN_ASSETS.md](docs/LEGACY_CHAIN_ASSETS.md)
