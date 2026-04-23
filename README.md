# xian-abci

`xian-abci` is the CometBFT-facing Xian node runtime. It owns deterministic
chain execution, ABCI request handling, state export and snapshot flows, and
node-adjacent services such as BDS, metrics, and the optional dashboard.

The published PyPI package name is `xian-tech-abci`. The console entrypoints
remain `xian-abci`, `xian-dashboard`, and the other `xian-*` commands exposed
by this repo.

## Scope

This repo owns:

- the ABCI application and request handlers
- transaction execution, rewards, validators, and query behavior
- node setup, config rendering, genesis building, and state export/import
- state snapshots, BDS indexing, and backend-oriented maintenance CLIs

This repo does not own:

- the operator-facing UX surface in `xian-cli`
- Docker and local stack orchestration in `xian-stack`
- remote host deployment in `xian-deploy`
- committed network-specific chain assets in `xian-configs`

## Workspace Assumptions

Local `uv` development uses editable sibling checkouts.

- required for normal development: `../xian-contracting`
- used by full validation or manual tooling in this repo: `../xian-contracts`,
  `../xian-configs`, and `../xian-stack`

If you want a ready-made local runtime instead of wiring the node yourself, use
`xian-stack`. This repo focuses on the runtime and backend tooling, not the
full localnet UX.

## Common Commands

Bootstrap the development environment:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
```

Inspect the command surface:

```bash
uv run xian-abci --help
uv run xian-dashboard --help
uv run xian-configure-node --help
uv run xian-export-state --help
uv run xian-state-snapshot --help
uv run xian-bds-reindex --help
uv run xian-bds-snapshot --help
uv run xian-bds-spool --help
```

The `vm` extra enables the `xian_vm_v1` bindings. The `native` extra enables
the native tracer and admission helpers.

## Repository Layout

- `src/xian/`: repo-owned node runtime, services, utilities, and CLI entrypoints
- `src/abci/`: lower-level ABCI server and protocol glue
- `protos/`: vendored CometBFT schemas plus supporting protobuf dependencies
- `build_proto.py`: regenerates checked-in protobuf Python stubs under `src/`
- `scripts/`: repo-level validation and manual benchmark entrypoints
- `tests/`: unit, ABCI-method, integration, governance, and system coverage
- `docs/`: architecture notes, design docs, and backlog tracking

## Validation

The preferred full validation entrypoint is:

```bash
./scripts/validate-release.sh
```

`./scripts/validate-release.sh` wraps the repo validation used for releases. It
runs:

- defaults to Python `3.14` unless `XIAN_ABCI_VALIDATE_PYTHON` is set
- `./scripts/validate-repo.sh`
- protobuf regeneration / stale-stub checks
- the Python-vs-native processor fuzz parity coverage under
  `tests/integration/test_vm_processor_fuzz.py`

CI provisions Postgres for the BDS-backed paths. To mirror that locally, make
Postgres available at `postgres://postgres:1234@localhost:5432/xian`.

## Scripts And Generators

- `scripts/validate-release.sh`: release-grade validation wrapper
- `scripts/validate-repo.sh`: full local and CI validation entrypoint
- `scripts/benchmark_shielded_chi.py`: manual shielded-fee benchmark harness;
  it is intentionally not part of the default validation path and expects the
  sibling `xian-contracting` and `xian-contracts` repos to be present
- `build_proto.py`: protobuf stub generator used by validation

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/README.md](docs/README.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/CHAIN_ASSETS.md](docs/CHAIN_ASSETS.md)
- [docs/SAFETY_INVARIANTS.md](docs/SAFETY_INVARIANTS.md)
