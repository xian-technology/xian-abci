# Safety Invariants

This file records the runtime properties that must hold before an
`xian-abci` release is considered safe.

## Determinism

- The same transaction sequence must produce the same transaction results,
  events, and committed state on every native V2 node. Speculative parallel
  execution must match serial execution.
- Validator replay must converge on the same app hash and contract state after
  restart, catch-up, and proposal processing.
- Block time, block hash, and chain-id handling must remain deterministic across
  nodes.
- Parallel execution must include fee and reward configuration reads in its
  conflict checks, including cached chi prices. A configuration update earlier
  in a block must affect later transactions exactly as it does in serial
  execution.

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
  startup refuse anything else.
- Shielded proof-cache warming is optional acceleration. Rejected warm-up
  requests must not fail a block or bypass verification during execution.
- Replay after failure must leave the same committed state on every node.
- A committed block's height and app hash must become durable in the same LMDB
  transaction as its state writes. `Info` must never advertise metadata for
  state that was not committed, and startup must repair the compatibility JSON
  mirror from the authoritative LMDB commit marker after a crash. Mirror I/O
  failure must be logged but must not invalidate an authoritative LMDB commit.
- Fee-accounting balance writes may still apply on failure when the runtime
  policy intentionally charges failed execution, but they must remain
  deterministic across nodes and runtimes.

## Validator Selection

- Consensus identities are exactly 32-byte hexadecimal keys. Case variants
  compare as one key without changing the spelling of contract account keys.
  Equal-power aliases collapse to one validator; conflicting powers reject the
  selection independently of list ordering.
- Malformed identities are excluded. An empty or entirely invalid selection
  retains the last effective nonempty consensus set, including across blocks,
  restart, export, and state sync.
- Node-managed `validators.__consensus__` records that effective set in the
  same committed state and app hash as the block. Contracts cannot declare
  this reserved metadata name or write it through foreign state access.
- Existing state without this metadata bootstraps from its committed valid
  active validators. This is an upgrade boundary, not recovery for a chain
  whose previous contract state already disagrees with CometBFT.

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
- `tests/integration/test_parallel_executor.py`
- `tests/unit/test_execution_engine.py`
- `tests/unit/test_execution_revision.py`
- `tests/unit/test_state_root.py`
- the ABCI-method integration tests under `tests/abci_methods/`
