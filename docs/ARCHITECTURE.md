# Architecture

`xian-abci` is the deterministic node runtime that sits between CometBFT and
the contract engine.

Main areas:

- `src/xian/xian_abci.py`: runtime assembly, startup wiring, and ABCI dispatch
- `src/xian/methods/`: ABCI request handlers and query surfaces
- `src/xian/processor.py`, `execution_engine.py`, `execution_policy.py`,
  `parallel_executor.py`, and `parallel_planner.py`: transaction execution flow
- `src/xian/services/`: BDS and snapshot/state-sync services
- `src/xian/dashboard/`, `metrics.py`, `simulator.py`, and
  `vm_observability.py`: optional node-adjacent surfaces
- `src/xian/utils/state_patches.py` and `src/xian/tools/state_patches/`:
  governed state-patch loading and execution
- `src/xian/node_setup.py` and `src/xian/node_admin.py`: reusable home/config
  bootstrap helpers and archive restore flows
- `src/xian/cli/`: backend-oriented command entrypoints
- `tests/`: unit, integration, governance, and system coverage

Dependency direction:

- consumes `xian-contracting` for execution semantics
- consumes `xian-runtime-types` for shared deterministic values
- consumes `xian-configs` for canonical contract bundles and network assets
- is consumed by `xian-cli` and `xian-stack`
