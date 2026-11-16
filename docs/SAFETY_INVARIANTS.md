# Safety Invariants

This file records the runtime properties that must hold before an
`xian-abci` release is considered safe.

## Determinism

- The same transaction sequence must produce the same transaction results,
  events, and committed state on every native V2 node. Blocks execute serially
  in canonical transaction order.
- Validator replay must converge on the same app hash and contract state after
  restart, catch-up, and proposal processing.
- Block time, block hash, and chain-id handling must remain deterministic across
  nodes.

## Failure Semantics

- Failed transactions must not persist contract writes or events.
- Successful VM effects must be representable by the ABCI/state-root encoder,
  with UTF-8 storage keys, before any contract writes or events are applied.
  An incompatible effect fails the whole transaction and discards its contract
  writes and events while retaining consumed chi for normal failure accounting.
  This check does not restrict return-only values: transaction results continue
  to render those values as text.
- Paid and free metered transactions require a positive `chi_supplied` at
  admission, proposal validation, and finalization. Internal system execution
  retains its explicit unmetered policy. Changes to these consensus validation
  rules require validators to upgrade together.
- Every chain fee mode carries hard chi caps in the hashed genesis entry
  `__fee_policy` (`tx_fee_mode`, `max_tx_chi`, `max_block_chi`; defaults
  200,000 and 500,000 chi). Chi is the only deterministic bound on execution
  time, so these caps bound how long one transaction, and one block, can delay
  every later transaction. CheckTx rejects a transaction whose `chi_supplied`
  exceeds `max_tx_chi`; PrepareProposal packs transactions only while the sum
  of `chi_supplied` stays within `max_block_chi`; ProcessProposal rejects a
  block that breaks either cap, and FinalizeBlock rejects such transactions
  before execution. A policy is valid only when
  `0 < max_tx_chi <= max_block_chi <= 10,000,000` and `max_tx_chi` does not
  exceed the VM's own per-transaction ceiling; genesis building and node
  startup refuse anything else. A governed state patch may replace the policy
  only with one that passes the same check: a node refuses to load a bundle
  that writes an invalid or non-canonical `__fee_policy` or deletes it, because
  every validator would commit it and then fail to read it. A patched policy
  takes effect at the Commit of its activation block, so every stage of the
  next block uses it on running and restarted nodes alike. Bundles cannot
  write `__execution_policy` at all.
- The block cap counts supplied chi, so supplied chi that goes unused must
  cost the sender: in paid mode the Rust executor charges `used + (chi_supplied
  - used) // 2` (the unused-chi charge, revision 14) and reports it as the
  receipt's `chi_used`. Fee rewards are computed on `used` alone; the charge is
  burned, so no validator or developer earns from wasted block capacity and no
  token is created. Without it, a few transactions that supply far more than
  they use fill every block for almost nothing and keep honest transactions
  out. The simulator settles its estimate as execution would settle a
  transaction supplying it (reservation, refund, unused-chi charge), so a
  supply of the estimate is charged what the estimate reports.
- The chain chi rate (`chi_cost.S:value`) prices every fee. Admission,
  execution (the Rust executor) and simulation refuse a rate that is not an
  integer in `1..=10**61 - 1` (the node halts rather than guess a price), so
  nothing may write one: genesis building refuses a bundle that seeds an
  invalid rate, a node refuses to load a state patch bundle that writes or
  deletes it, and the `chi_cost` contract accepts only positive rates.
- A runtime feature (`__runtime_features.<feature>`) is chain state, read on
  every contract preparation and deployment, never from a node-local copy. A
  governed state patch may switch a supported feature on or off, but only to a
  JSON boolean (what genesis writes); a node refuses to load a bundle that
  writes another value, an unknown feature or deletes the key. A node never
  executes blocks under a feature it cannot support: startup, snapshot import
  and Commit refuse a zk-enabled state without the native zk verifier, and a
  node without the verifier halts at a zk-enabling patch's activation block
  before computing its state root, so its committed state stays untouched until
  the verifier is installed and the block is replayed. While zk is off, every
  transaction that deploys or calls a contract using zk host syscalls (directly
  or through an import) fails with an ordinary failed receipt and charge; it
  never faults or halts the node. The switch takes effect at the Commit of the
  activation block on running and restarted nodes alike.
- Shielded proof-cache warming is optional acceleration. Rejected warm-up
  requests must not fail a block or bypass verification during execution. It
  is skipped while the chain's zk feature is off.
- Replay after failure must leave the same committed state on every node.
- A host fault during FinalizeBlock discards every pending block write and
  staged root before the ABCI server shuts down all connections. A partial
  block must not become visible to later queries or be committed on retry.
  Shutdown cancellation must not recursively start another global shutdown.
- A committed block's height and app hash must become durable in the same LMDB
  transaction as its state writes. `Info` must never advertise metadata for
  state that was not committed, and startup must repair the compatibility JSON
  mirror from the authoritative LMDB commit marker after a crash. Mirror I/O
  failure must be logged but must not invalidate an authoritative LMDB commit.
- Fee-accounting balance writes may still apply on failure when the runtime
  policy intentionally charges failed execution, but they must remain
  deterministic across nodes and runtimes.
- A charged fee never disappears silently. In the paid fee mode a failed
  transaction's fee is paid out through the governed reward split exactly like
  a successful one's, except that the developer share goes to the validators.
  When the sender cannot cover the failure fee, the whole balance is charged
  and paid out the same way. Paid out plus burned equals the charged fee
  exactly: shares are computed in `ContractingDecimal` and truncated, so they
  never exceed the fee and none is negative; the governed burn share and the
  truncation dust leave the native supply. A split whose paid ratios are
  negative or exceed one pays no rewards (the fee is burned). The Rust block executor computes this; the receipts and state
  are pinned by the golden fixtures frozen from the former Python path
  (`tests/fixtures/golden`, the fee and reward corpora).

## Validator Selection

- Consensus identities are exactly 64 lowercase ASCII hexadecimal characters
  encoding an Ed25519 public key. Other spellings are rejected at registration
  and excluded from consensus updates, so one signing key has one account name.
- Malformed identities are excluded. An empty or entirely invalid selection
  retains the last effective nonempty consensus set, including across blocks,
  restart, export, and state sync.
- Node-managed `validators.__consensus__` records that effective set in the
  same committed state and app hash as the block. Contracts cannot declare
  this reserved metadata name or write it through foreign state access.
- Existing state without this metadata bootstraps from its committed valid
  active validators. This is an upgrade boundary, not recovery for a chain
  whose previous contract state already disagrees with CometBFT.

## Validator Penalties And Queries

- The agreed block's evidence and liveness are applied before user
  transactions, so a claim in that block cannot withdraw stake before its
  penalty freezes it. Fee rewards therefore use the membership after those
  immediate jails. Pending penalties then share one bounded continuation call
  after user transactions and before automatic epoch selection.
- Pending penalty jobs, their FIFO order and every continuation cursor are
  consensus state. Restart or replay resumes the committed cursor. Processing
  visits only its bounded batch per block and uses no local work queue.
- Historical evidence resolves a consensus address through
  `validators.consensus_keys:<address>`. The resolved public key is checked
  against the address before a penalty is applied. Liveness only scans the
  bounded active set, never the lifetime registry.
- The pending-unbond page route executes one bounded contract call. The
  legacy list route returns the same complete list only when it fits in one
  page; larger lists return an explicit pagination error rather than blocking
  consensus with one unmetered VM call per historical record.

## Readonly Execution And Operator Identity

- Readonly simulation uses the latest committed height H and timestamp,
  rather than a guessed H+1 or wall-clock time. Its committed LMDB snapshot,
  pending overlay, nonce, runtime-feature fallback and metadata are captured
  under the state-transition lock and kept consistent through all estimation
  rounds. A map resize invalidates the snapshot explicitly and requires a new
  query; it must not silently mix committed versions.
- Simulation capacity includes prepared readers and running workers. A
  timed-out worker keeps its slot until it actually finishes; abandoned
  preparations release their slots. Native pinned-reader limits retain
  headroom for ordinary consensus reads.
- BDS database queries run after releasing the consensus state lock. A slow
  or unavailable indexer cannot hold CheckTx, FinalizeBlock or Commit while
  waiting for PostgreSQL. Status queries capture the node height first.
- Materializing or overwriting an existing node cannot recreate a missing
  validator signing-state file or silently change its configured location.
  Initialization refuses before writing configuration or keys and requires
  restoration of the original signing high-water mark.
- Private key and configuration writes stage owner-only files and install
  them atomically. A failed write preserves the original file; an initial
  write refuses to replace an identity created concurrently.

## Native Runtime Validation

- The node accepts only its exact native V2 execution policy. Persisted state,
  genesis, snapshots, and bindings must agree on the execution revision, gas
  schedule, bytecode version, and set codec.
- Result, event, state, or app-hash disagreement between validators is
  release-blocking until explained and accepted.
- Release validation must include a real localnet run that checks consistent
  app hashes and contract state across nodes, including restart and replay.

## Release Gate

Use `./scripts/validate-release.sh` for repo-local release validation and
`xian-stack/scripts/release-safety.sh` for the full cross-repo localnet gate.

The enforcement surface in this repo includes:

- `tests/integration/test_vm_replay_parity.py`
- `tests/integration/test_vm_processor_fuzz.py`
- `tests/unit/test_execution_engine.py`
- `tests/integration/test_rust_block_executor.py` and the golden fixtures in
  `tests/fixtures/golden` (outputs frozen from the former Python path)
- `tests/unit/test_execution_revision.py`
- `tests/unit/test_state_root.py`
- `tests/unit/test_state_root_from_storage.py` (the Rust rebuild against the
  frozen Python reference treap)
- the ABCI-method integration tests under `tests/abci_methods/`
