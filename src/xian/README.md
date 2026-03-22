# xian

## Purpose
- This package contains the main Xian node runtime.

## Contents
- `methods/`: ABCI request handlers
- `services/`: BDS, dashboard, metrics, state sync, and other subsystems
- `cli/`: backend entrypoints
- `node_setup.py` and `node_admin.py`: reusable node-home helpers

## Notes
- This is the main architectural boundary in the repo. Changes here often affect node behavior directly.

## Next
- Open `methods/` for request flow or `services/` for background/runtime subsystems.

