# Safety Invariants

This file records the runtime properties that must hold before an
`xian-abci` release is considered safe.

## Determinism

- The same transaction sequence must produce the same transaction results,
  events, and committed state under the Python and native execution paths.
- Validator replay must converge on the same app hash and contract state after
  restart, catch-up, and proposal processing.
- Block time, block hash, and chain-id handling must remain deterministic across
  nodes.

## Failure Semantics

- Failed transactions must not persist contract writes or events.
- Replay after failure must leave the same committed state on every node and in
  every execution engine.
- Fee-accounting balance writes may still apply on failure when the runtime
  policy intentionally charges failed execution, but they must remain
  deterministic across nodes and runtimes.

## Native Runtime Rollout

- Native-authoritative execution must continue to produce shadow-comparison data
  for mismatch detection.
- Any native/shadow mismatch is release-blocking until explained and accepted.
- Release validation must include a real localnet run that enforces a zero
  mismatch budget.

## Release Gate

Use `./scripts/validate-release.sh` for repo-local release validation and
`xian-stack/scripts/release-safety.sh` for the full cross-repo localnet gate.

The enforcement surface in this repo includes:

- `tests/integration/test_vm_replay_parity.py`
- `tests/integration/test_vm_processor_fuzz.py`
- `tests/unit/test_processor_shadow.py`
- `tests/unit/test_vm_observability.py`
- the ABCI-method integration tests under `tests/abci_methods/`
