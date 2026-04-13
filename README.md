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
uv run xian-legacy-replay-audit --help
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
  - `xian-legacy-replay-audit`

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
./scripts/validate-repo.sh
```

Optional runtime extras:

- `uv sync --extra native` for the native admission/tracer helpers
- `uv sync --extra vm` for the experimental `xian_vm_v1` native-runtime path
  with stored-IR-first preflight plus native execution wiring on explicit
  simulation requests and on the real tx path

The current `xian_vm_v1` rollout model is intentionally strict:

- `xian_vm_v1` is native-authoritative only on this branch; there is no
  Python shadow/compare mode in node execution
- native contract deployment is artifact-driven:
  `submission.submit_contract(...)` calls must carry `deployment_artifacts`
  with persisted `vm_ir_json` instead of relying on a source-only compile path
- native deployment is also deterministic-context-driven:
  the native deploy path requires explicit `now`/block context from the node
  runtime and will not fall back to local wall-clock time
- `xian_vm_v1` execution is strict about artifacts:
  contracts must already carry persisted `__xian_ir_v1__`; stored
  `__source__` remains available for inspection, but it is not used as a
  runtime fallback
- VM-native state stores `__source__` plus `__xian_ir_v1__`; `__code__` is
  not part of the native deployment/runtime path
- the node does not silently try native first and then hide problems behind a
  fallback to Python
- transaction simulation remains explicit client-triggered behavior; the node
  does not auto-run simulation for every incoming transaction
- VM shadow/native observability is exported through the Prometheus endpoint:
  - `xian_node_info` now includes execution mode, authority, shadow flag,
    bytecode version, and gas schedule
  - `xian_vm_shadow_metric` and `xian_vm_shadow_stage_metric` expose
    comparison and mismatch counters
  - `xian_vm_shadow_last_mismatch_info` exposes the latest mismatch context
- when VM comparison is active, mismatch records are also appended to:
  `storage/logs/xian-vm-shadow-mismatches.jsonl`

Legacy network replay audit is now available as an explicit backend tool:

```bash
uv run --extra vm xian-legacy-replay-audit \
  --rpc-url https://node.xian.org \
  --graphql-url https://node.xian.org/graphql \
  --output-dir ./.artifacts/legacy-replay \
  --logic-only \
  --native-only \
  --max-transactions 100
```

That tool is intentionally split into two views:

- strict historical parity:
  uses the historical chi budget and current rewards path, so it highlights
  legacy-vs-current economic drift directly
- logic parity:
  replays with fees and rewards disabled and compares only
  `status/result/events`, so contract execution compatibility is visible even
  when legacy fee calibration differs from the current stack
- native-only:
  skips the current Python replay path and focuses only on whether
  `xian_vm_v1` can process the historical transactions

The replay tool seeds from the live legacy chain `GENESIS` pseudo-transaction,
reads ordered transactions from CometBFT RPC block data, and writes:

- `report.json`
- `transactions.jsonl`
- `contract_inventory.json`
- `contract_compatibility.json`
- a local replay state directory under `output-dir/replay-state`

The BDS-backed test paths expect Postgres at
`postgres://postgres:1234@localhost:5432/xian`.

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/README.md](docs/README.md)
- [docs/LEGACY_CHAIN_ASSETS.md](docs/LEGACY_CHAIN_ASSETS.md)
