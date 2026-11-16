# Snapshots And Pruning

`xian-abci` has three separate snapshot workflows, and they solve different
problems:

1. full-home archive restore
2. CometBFT state sync with Xian application snapshots
3. BDS snapshot export/import for indexed Postgres state

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
- stop the node before restoring; concurrent restore commands are rejected
- only regular files and directories under `data/` and `xian/` are restored;
  other node-home entries are ignored, while traversal paths, links, special
  files, duplicate file paths and conflicting layouts are rejected
- the existing validator signing state and node/validator identity keys are
  retained, using both default paths and configured paths in `config/config.toml`;
  an archive cannot supply a missing local identity or signing-state file
- symlinked application roots or protected paths inside those roots are rejected;
  identity links outside the replaced roots are retained and their targets are protected
- extraction and validation finish in staging before application data is replaced;
  publication failures roll back both directories

Restoring both directories is not a single filesystem transaction. If the
process stops during publication, or rollback cannot complete, original data
is retained under `.snapshot-restore-*/backup/`. Another restore is blocked
while that recovery copy exists. Keep the node stopped, recover the original
directories from the backup (including signing state), and remove the staging
directory only after verifying recovery. Never reset the signing high-water
mark to match an older snapshot.

## CometBFT State Sync Snapshots

Xian also implements the ABCI snapshot lifecycle required for CometBFT state
sync:

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
- contract key/value state
- nonce state

The current application hash is a raw 32-byte `xian-state-root-v3` Merkle root
over the canonical Xian consensus key/value state. The root includes contract
state and committed nonce keys such as `__n.<sender>`, and excludes local runtime
keys that are not part of consensus state.

State-root leaves are domain-separated and hash each key with the exact bytes
LMDB stores for its value: the canonical V2 storage encoding, which the Python
and Rust encoders write byte-identically. A leaf therefore commits to the value
and its type (a datetime and its text, or bytes and equal text, are different
leaves). A stored JSON `null` counts as an absent key. Leaves are organized in a
deterministic Merkle treap keyed by state key with cryptographic priorities
derived from those keys. During block finalization, Xian updates an in-memory
root cache from the block's pending writes, hashing the same bytes Commit then
stores (`Driver.pending_stored_items`), instead of scanning the whole database.
The cache is rebuilt from committed state on startup, genesis, and state-sync
import. The treap and its hashing live in vm-core (`xian_vm_core.StateRootTree`);
a rebuild reads committed LMDB bytes in Rust with the GIL released and does not
decode values. Exported state is hashed by encoding each value as the node
stores it. V1 state keeps the rules of its pinned archival runtime.

On import, Xian rebuilds:

- LMDB state
- nonce keys
- latest block height/hash metadata

Before import, Xian recomputes the state root from `exported_state.json` and
rejects the snapshot if it does not match the advertised application hash.

Imported or exported application snapshots can then be served back to peers
through the CometBFT snapshot lifecycle.

### Verified Comet History And Local Pairing

Fresh-chain setup targets the history-aware CometBFT 0.40 backend. Application
state alone does not restore the historical headers and validator sets needed
to validate still-eligible evidence from before a checkpoint. The compatible
client must authenticate and restore that history before bootstrapping Comet
state or starting consensus. It authenticates evidence limits through the
local committed `/evidence_policy` query and trusted checkpoint application
root; Comet's consensus-parameter hash does not cover evidence limits.

The ABCI facade accepts snapshot offers only from the matching local client.
A private Echo registration creates one ephemeral, one-use capability; the
client wraps the offer metadata locally, and the app unwraps it before normal
snapshot validation. Stock clients and unregistered, stale or replayed wrappers
are rejected. Public Info/query calls and peer-supplied flags cannot register
this capability. The nonce never enters chain state, logs or served metadata.
Peer snapshot metadata and archives retain their original format.

This gate does not replace history verification. The paired client must reject
unverifiable history and must not fall back to unsafe bootstrap. Trusted offline
manager/CLI APIs remain available; an application-only restore still needs the
matching Comet history. Snapshot replacement and committed-policy queries use
the same state-transition lock.

The hashed `__evidence_policy` record preserves the complete evidence triple,
a monotonic target and the exact committed time anchor. Genesis covers its
seeded unbonding period plus one day. Later increases grow effective duration
by at most elapsed committed time, so the old expiry cutoff never moves
backward. Reductions never shrink protection for existing claims. Recovery
reanchors to its new genesis time without credit for the halted interval.


### Local Resource Limits

Application snapshot handling defaults to a 256 MiB compressed archive and a
256 MiB exported-state file, with at most 64 KiB of metadata. Offers are checked
before JSON decoding or staging; chunk counts and cumulative bytes are bounded.
Gzip expansion and auxiliary tar-header reads are limited before extraction.
Only the two expected regular files are accepted. Disk-space checks reserve
16 MiB for other work, and failed incoming transfers remove their staging data.

These are local service limits, not consensus rules or a maximum chain size.
Set the following top-level values in `config/xian.toml` when a legitimate
snapshot requires a larger local budget:

```toml
snapshot_max_state_bytes = 536870912
snapshot_max_archive_bytes = 536870912
snapshot_max_metadata_bytes = 65536
snapshot_min_free_disk_bytes = 16777216
```

The snapshot CLI reads the same settings. `--max-state-bytes` and
`--max-archive-bytes` override them for one command. Imported state is still
materialized and verified in memory; the byte cap is not a RAM ceiling. Choose
the limit and container memory together before raising the cap.

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

Peer state sync still depends on CometBFT's trusted height, trusted hash, and
RPC trust-period model. Once that trusted header is accepted, Xian verifies that
the downloaded snapshot contents recompute to the trusted CometBFT `app_hash`.
Operator-distributed snapshots can still add signed manifests for provenance
and transport integrity, but snapshot contents are no longer trusted solely
because a provider served them.

## Current State-Sync Model

The current implementation is intentionally conservative:

- one supported snapshot format: `2`
- manual snapshot export
- chunked peer serving from locally exported or imported snapshots
- strict full-state import
- deterministic full-state Merkle-root verification
- incremental per-block state-root updates
- no automatic periodic snapshot generation yet
- no compact Merkle inclusion proofs yet

This keeps the application state-sync path explicit and auditable.

## BDS Snapshots

BDS snapshots are a separate operator tool for the indexed Postgres state.
They are not used by CometBFT state sync and they do not replace LMDB or full
node-home restore.

Use the dedicated BDS snapshot CLI:

```bash
uv run xian-bds-snapshot export
uv run xian-bds-snapshot export --output-path ./xian-bds-snapshot.tar.gz
uv run xian-bds-snapshot import --input-path ./xian-bds-snapshot.tar.gz
uv run xian-bds-snapshot import --input-path ./xian-bds-snapshot.tar.gz --clear-spool
```

Important properties:

- exports and imports the BDS schema and indexed chain data
- helps bootstrap or recover the local index faster
- validates imported indexed-head metadata against a trusted RPC block source
- can optionally clear the local spool before import
- does not affect consensus or the application snapshot lifecycle

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
