# Architecture

`xian-abci` is the deterministic node runtime that sits between CometBFT and the contract engine.

Main areas:

- `src/xian/methods/`: ABCI request handlers and query surfaces
- `src/xian/services/`: BDS, dashboard, metrics, state sync, and related services
- `src/xian/node_setup.py` and `src/xian/node_admin.py`: reusable home/config bootstrap helpers
- `src/xian/cli/`: backend-oriented command entrypoints
- `tests/`: unit, integration, governance, and system coverage

Dependency direction:

- consumes `xian-contracting` for execution semantics
- consumes `xian-runtime-types` for shared deterministic values
- is consumed by `xian-cli` and `xian-stack`

