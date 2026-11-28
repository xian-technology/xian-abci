# Snapshots And Pruning

Fresh chains use unmodified upstream CometBFT. A node can replay from genesis
or restore a verified, coherent full-node-store backup. P2P application state
sync is unsupported: an application-only checkpoint omits native historical
headers, commits and validator sets required for still-valid evidence.

## Verified Full-Node-Store Restore

`xian.node_admin.apply_snapshot_archive(...)` is the common bootstrap verifier
used by operator tools. Stop both ABCI and CometBFT before restoring. The
recipient must already have its own `config/config.toml`, public genesis and
identity/signing-state files. The archive replaces only `data/` and `xian/`;
recipient configuration, node keys, validator keys and signing state survive.
An archive cannot supply missing local signing state. Do not reset a validator's
signing high-water mark to match an older checkpoint.

Ed25519 publishers sign a v2 JSON manifest with this schema.

```json
{
  "manifest_version": 2,
  "snapshot_kind": "full_node_store",
  "chain_id": "example-local-chain",
  "height": 123,
  "genesis_sha256": "<64 lowercase hex>",
  "app_hash": "<64 lowercase hex>",
  "block_hash": "<64 lowercase hex>",
  "snapshot_url": "https://publisher.example/store.tar.gz",
  "snapshot_sha256": "<64 lowercase hex>",
  "snapshot_bytes": 12345,
  "uncompressed_bytes": 81920,
  "signing_public_key": "<64 lowercase hex>",
  "signature": "<128 lowercase hex>"
}
```

All fields except `signature` are signed using compact, sorted JSON produced by
`full_node_store_manifest_payload`. Byte counts are positive integers;
`uncompressed_bytes` is the complete TAR stream including headers and padding.
The signer must be independently trusted. The manifest is limited to 64 KiB;
v1 manifests and application-only backups cannot authorize bootstrap.

ML-DSA-65 publishers use the same fields with `manifest_version: 3`.
`signing_public_key` is `ml-dsa-65:` followed by 3,904 lowercase hex characters.
`signature` is 6,618 lowercase hex characters. The signer prepends the ASCII
bytes of `XIAN-FULL-NODE-STORE` and one zero byte to the UTF-8 canonical JSON
and signs the result with pure ML-DSA-65 and an empty FIPS 204 context.

Create the publisher key offline with `python -m xian.snapshot_attestation NEW_KEY_FILE`.
The command creates a mode-0600 file exclusively and prints its public pin.
Distribute that pin independently to the recipients' `snapshot_signing_keys`.
Rotate by preinstalling a second independently authenticated PQ pin, publishing
and verifying a snapshot with that key, then removing the retired pin everywhere.
The pin authenticates the publisher. For a specific historical recovery point,
pin the expected archive SHA-256 independently as well. An older valid snapshot
of the same chain can still authenticate.

A PQ pin requires a signed v3 manifest. The recipient also requires a PQ
publisher when its genesis enables ML-DSA-65 validators or an account registry,
or when its committed store or the incoming store contains the account registry.
For a registry activated after genesis, provision PQ pins for every fresh
recipient before bootstrap. This carries the upgraded trust policy to an empty
home. Both local `file://` manifests and HTTP(S) manifests use these checks.
Trust the genesis, runtime and publisher pins through independent channels.

A classical home can use an independently trusted `expected_sha256` for a raw
full-store archive. Every restore verifies the complete checkpoint, genesis,
history and resource limits. Verification authenticates the publisher's
checkpoint without replaying every transaction from genesis.

Before replacing either root, the verifier:

- streams and checks the archive checksum and exact signed byte counts;
- rejects unsafe TAR paths, links, special files, duplicate names and conflicting
  layouts, and excludes configured/default recipient identities;
- recomputes the actual LMDB consensus root and compares it with the atomic
  committed height/hash/time marker and signed application hash;
- runs `xian-node-store-inspect` against existing read-only `goleveldb` stores,
  binding native state, block checkpoint, application root, exact time and full
  evidence limits to the same height;
- compares the native cached GenesisDoc with the recipient genesis through
  upstream types, and checks the signed full-document canonical genesis SHA;
- verifies complete native header, commit and validator metadata throughout the
  live evidence window, including an authenticated expired boundary or initial
  height. Evidence expires only after BOTH block and time limits are exceeded.
  Bodies may be pruned if this proof metadata remains available.

The default inspector runs:

```bash
xian-node-store-inspect --db-dir <staged-data> --db-backend goleveldb \
  --genesis-file <recipient-genesis> --max-history-heights 1000000 --timeout 120s
```

`native_store_inspector(db_dir, db_backend, genesis_file)` can supply the same
check through a selected image when the host has no installed inspector.
The shared inspection policy reads
`XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS` and
`XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS`. Raise these for legitimate longer
windows; exhausting a budget fails closed. Other native database backends can
still replay, but verified full-store restore currently supports only goleveldb.

`FullNodeStoreLimits` exposes operator-adjustable limits: 8 GiB compressed,
32 GiB uncompressed TAR, 100,000 members and 256 MiB free-disk reserve. Pass
`limits=` to the restore API for larger stores. Signed sizes are checked before
download; decompression, extraction and disk space are checked during writes.
These are local resource policies, not consensus chain-size ceilings.

Extraction and verification finish in private staging. Publication of the two
roots rolls back on Python errors, but the two renames are not one filesystem
transaction. A durable publication marker and retained backup block startup,
export and another restore after interruption, including a first restore with
no old roots. Keep the node stopped; recover the original directories from
`.snapshot-restore-*/backup/` where present, verify both roots and signing state,
and only then remove the interrupted workspace. Never delete the recovery copy
to bypass the guard.

## P2P Application State Sync Is Disabled

Managed configs set `[statesync].enable = false`. Configuration and startup
reject an enabled or malformed setting in a hand-edited `config.toml`. The
ABCI facade advertises an empty snapshot list, returns `ABORT` for snapshot
offers and chunk application, and serves empty chunk responses.

## Offline Application Exports

`xian.services.state_snapshot.StateSnapshotManager` and `xian-state-snapshot`
list, export, and import trusted application state offline. Format-2 archives
contain only `metadata.json` and `exported_state.json`. Use a verified full
node-store archive to restore both native Comet databases and application state.

```bash
uv run xian-state-snapshot list
uv run xian-state-snapshot export --output-path ./application.tar.gz
uv run xian-state-snapshot import --input-path ./application.tar.gz
```

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
The cache is rebuilt from committed state on startup, genesis, and offline application-state
import. The treap and its hashing live in vm-core (`xian_vm_core.StateRootTree`);
a rebuild reads committed LMDB bytes in Rust with the GIL released and does not
decode values. Exported state is hashed by encoding each value as the node
stores it. V1 state keeps the rules of its pinned archival runtime.

On offline import, exported state is fully verified before LMDB, nonces and the
committed block marker are replaced. Restart the application after import; this
does not provide matching native Comet stores.

Offline application exports retain their separate default limits: 256 MiB
archive, 256 MiB exported-state JSON, 64 KiB metadata and 16 MiB disk reserve.
The top-level `snapshot_max_state_bytes`, `snapshot_max_archive_bytes`,
`snapshot_max_metadata_bytes` and `snapshot_min_free_disk_bytes` values in
`config/xian.toml` configure these limits. The CLI additionally accepts
`--max-state-bytes` and `--max-archive-bytes`. Import materializes the JSON in
memory, so choose memory and byte limits together. No automatic periodic
application export is currently scheduled.

The hashed `__evidence_policy` schema2 record stores a base duration, exact
`growth_start_nanos`, monotonic target and full block-age/byte limits. Active
duration is derived at the committed checkpoint timestamp, so empty blocks do
not rewrite the app hash even during growth. Full-store verification compares
the derived active triple with native state, not the stored base or the latest
unbonding setting. See [time semantics](TIME_SEMANTICS.md) for safe target
adoption, idle behavior and recovery's required independent checkpoint.


## BDS Snapshots

BDS snapshots restore the indexed Postgres state separately from the application
and consensus stores.

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

BDS indexes blocks in height order. If an earlier block is missing, the pending
queue reserves space for it while later live blocks remain recoverable from the
spool. On shutdown, BDS waits up to one second for queued and in-flight writes.
Any remaining spooled blocks can be replayed when indexing starts again.
The offline `xian-bds-spool drain --offline` command resumes replay from the
indexed height in Postgres and reports a timeout if entries are still spooled
when its time limit expires.

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

A verified full-store backup can retain pruned bodies if complete live evidence
proof metadata remains available. Replay and reindexing still require the
corresponding block bodies, locally or from an appropriate archival source.
Application-only exports do not replace either requirement.
