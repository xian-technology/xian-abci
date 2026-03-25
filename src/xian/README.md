# xian

## Purpose

This package contains the main Xian node runtime and the backend-oriented
subsystems around it.

## Contents

- `xian_abci.py`: the ABCI application entrypoint and request dispatch layer
- `methods/`: request handlers for `CheckTx`, `FinalizeBlock`, query, proposal
  validation, and related ABCI methods
- `services/`: BDS, dashboard, metrics, state sync, snapshots, and other
  node-adjacent services
- `cli/`: backend entrypoints owned by this repo
- `node_setup.py` and `node_admin.py`: node-home creation and configuration
  helpers
- `genesis_builder.py` and `state_export.py`: genesis, export, and restore
  primitives
- `parallel_executor.py` and `processor.py`: transaction execution planning and
  block-processing support

## Notes

- This is the main architectural boundary in the repo. Changes here often
  affect node behavior directly.
- `methods/`, execution flow, state export/import, and query behavior are
  especially consensus-sensitive.
- `services/` contains optional but important runtime layers such as BDS and the
  dashboard.

## Next

- Open `xian_abci.py` and `methods/` for transaction and request flow.
- Open `services/` for BDS, monitoring, dashboard, or snapshot/state-sync work.
- Open `cli/` when changing backend-oriented commands exposed by this repo.
