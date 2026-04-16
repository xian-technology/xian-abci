# Snapshots And Pruning

Xian now supports two different snapshot workflows, and they solve different
problems:

1. full-home archive restore
2. CometBFT state sync with Xian application snapshots

## Full-Home Archive Restore

This is the existing operator bootstrap path used by `xian-cli` and
`apply_snapshot_archive(...)`.

It restores a `.tar` or `.tar.gz` archive into the node home by replacing:

- `data/`
- `xian/`

This is useful when you already have a prepared node-home archive and want to
bootstrap a node directly from that archive.

Important properties:

- restores full local node state from a file or URL
- does not use CometBFT state sync
- does not require peer-served snapshot chunks
- remote restore should use either an explicit archive SHA256 or a signed
  snapshot manifest validated against trusted signing keys

## CometBFT State Sync Snapshots

Xian now also implements the ABCI snapshot lifecycle required for CometBFT
state sync:

- `list_snapshots`
- `offer_snapshot`
- `load_snapshot_chunk`
- `apply_snapshot_chunk`

The snapshot payload is an application-state archive, not a full node-home
archive. It contains:

- `metadata.json`
- `exported_state.json`

`exported_state.json` is the canonical exported Xian state:

- current application hash
- current height
- non-compiled contract state
- nonce state

On import, Xian rebuilds:

- LMDB state
- nonce keys
- latest block height/hash metadata

## Operator Tooling

Use the dedicated snapshot CLI to manage application snapshots:

```bash
uv run xian-state-snapshot list
uv run xian-state-snapshot export
uv run xian-state-snapshot export --output-path ./xian-state-snapshot.tar.gz
uv run xian-state-snapshot import --input-path ./xian-state-snapshot.tar.gz
```

Use `configure_node` state-sync settings when the node should consume snapshots
from peers:

- `--statesync-enable`
- `--statesync-rpc-server`
- `--statesync-trust-height`
- `--statesync-trust-hash`
- `--statesync-trust-period`

## Current State-Sync Model

The current implementation is intentionally conservative:

- one supported snapshot format: `1`
- manual snapshot export
- chunked peer serving from locally exported or imported snapshots
- strict full-state import
- no automatic periodic snapshot generation yet

This keeps the application state-sync path explicit and auditable.

## Pruning

Current Xian pruning is block-history pruning through `retain_height` in
`ResponseCommit`.

What pruning affects:

- local CometBFT history retained for RPC and replay

What pruning does not separately affect:

- the current LMDB application state

Operationally that means:

- pruned nodes still keep the latest Xian application state
- pruned nodes lose older block history over time
- local historical rebuilds become limited to the retained history window

## Snapshot And Pruning Interaction

- state sync lets a node bootstrap from a recent application snapshot
- pruning controls how much historical block data a node keeps afterward
- local reindex or replay workflows only work for heights the node still
  retains, unless an archival RPC source is available

If a network wants fast bootstrap plus minimal local history retention, the
recommended pattern is:

1. bootstrap from a recent application snapshot
2. use CometBFT state sync for the recent trusted height
3. enable pruning if the node is not meant to be archival
