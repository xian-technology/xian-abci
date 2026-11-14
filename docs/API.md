# API Surface

This file is a runtime-centric map of the query surface owned by `xian-abci`.
It is not a full user-facing API manual.

## Boundary

- Standard endpoints such as `/broadcast_tx_sync`, `/tx`, and `/status` come
  from CometBFT RPC, not from repo-local query routing.
- Repo-owned application queries are implemented in
  `src/xian/methods/query.py` and are reached through:
  `/abci_query?path="/..."`
- BDS-backed history and index queries are only available when the Blockchain
  Data Service (BDS) is enabled for the node runtime.

## Core Runtime Queries

These paths do not require BDS:

- `/health`
- `/ping`
- `/perf_status`
- `/get/<state-key>`
- `/get_next_nonce/<address>`
- `/contract_source/<name>`
- `/contract_ir/<name>`
- `/contract_methods/<name>`
- `/contract_vars/<name>`
- `/contract_info/<name>`
- `/contracts/limit=<n>/offset=<n>/sort=<submitted_at|name>/order=<asc|desc>`
- `/keys/<prefix>/limit=<n>/after=<cursor>`

Use `/get` and `/keys` for current raw application state. Use the contract
queries for deployed contract source, Xian VM IR, and metadata.

## Validator And Governance Queries

These runtime queries expose membership governance and state-patch scheduling
state:

- `/validators_policy`
- `/validators_active`
- `/validators_candidates`
- `/validators_validator/<account>`
- `/validators_pending_unbonds/<account>`
- `/validators_open_votes/limit=<n>/offset=<n>`
- `/validators_vote/<proposal-id>`
- `/validators_vote_records/<proposal-id>`
- `/state_patch_bundles`
- `/scheduled_state_patches/<height>`

## Simulation

Readonly simulation is exposed through:

- `/simulate_tx/<encoded_payload>`

The simulator uses the latest known block metadata when available so that
`now`, `block_num`, `chain_id`, and execution mode match real execution as
closely as possible.

Its chi budget is the chain's per-transaction cap `max_tx_chi` from
`__fee_policy`. The optional xian.toml `simulation_max_chi` can only lower it
(the budget is then `min(simulation_max_chi, max_tx_chi)`); it is unset by
default. A committed fee-policy change applies to later simulations. A dry run therefore
never reports success for more chi than CheckTx admits.

A simulation runs in the node process with the code a block runs (the Rust
`xian_vm_core.execute_tx`), over a fork of the pending state taken when the
query arrives; the fork is dropped afterwards. It runs on a worker thread
without the interpreter lock, so blocks keep going; its chi budget bounds the
work. Unlike a transaction, it does not reserve the fee budget from the
sender's balance, so a small balance still gets an estimate of the chi used.
`simulation_timeout_ms` bounds how long the query waits for the answer;
`simulation_max_concurrency` bounds how many run at once (a run keeps its slot
until it finishes, even after its query timed out).

## BDS-Backed Indexed Queries

When BDS is enabled, `xian-abci` also exposes indexed/history reads.

Block and transaction queries:

- `/blocks`
- `/block/<height>`
- `/block_by_hash/<hash>`
- `/tx/<hash>`
- `/txs_for_block/<height>`
- `/txs_by_sender/<address>`
- `/txs_by_contract/<contract>`
- `/addresses`

Operational BDS queries:

- `/bds_status`
- `/bds_spool`

Event and contract-summary queries:

- `/events`
- `/recent_events`
- `/events_for_tx/<hash>`
- `/dex_candles/<market_id>` (`source=xian_pairs_v1` by default)
- `/developer_rewards/<address>`
- `/contract_summary/<name>`

Indexed state and token queries:

- `/state/<prefix>`
- `/state_previous/<key>`
- `/token_contracts`
- `/token_balances/<address>`
- `/state_history/<key>`
- `/state_for_tx/<hash>`
- `/state_for_block/<height-or-hash>`

Shielded and state-patch queries:

- `/shielded_output_tags`
- `/shielded_wallet_history`
- `/state_patches`
- `/state_patches_for_block/<height>`
- `/state_patch/<hash>`
- `/state_changes_for_patch/<hash>`

## Staged Genesis Construction

`xian.genesis_builder.build_genesis_block()` builds and signs a complete bundle
in one call. Tools that need to add state between deployment and signing can
compose the same operations through the public helpers:

```python
from contracting.storage.driver import Driver
from xian.genesis_builder import (
    deploy_bundle_contracts,
    finalize_genesis_block,
    parse_genesis_time,
)

with Driver(storage_home=output_storage) as driver:
    deploy_bundle_contracts(
        driver,
        founder_private_key=founder_key,
        network=network,
        contracts_dir=contracts_dir,
        submission_time=parse_genesis_time(genesis_time),
    )
    driver.set("my_contract.initial_value", initial_value)
    block = finalize_genesis_block(driver, founder_private_key=founder_key)
```

Use an isolated driver for genesis construction. Deployment leaves writes
pending, including runtime feature flags, so callers can add or replace state.
`constructor_overrides` and `runtime_features` have the same meanings as in
`build_genesis_block()`. Finalization commits pending writes, exports sorted
state, computes its state root, and signs the exported state. Its optional
`number` argument defaults to `"0"`; explicitly passing
`founder_private_key=None` leaves the origin sender and signature empty.
The caller owns the driver's lifetime.

`normalize_genesis_time()` returns an ISO timestamp in UTC;
`parse_genesis_time()` returns the corresponding contract `Datetime`. Both
accept timezone-aware ISO text, reject naive timestamps, and sample the current
UTC time when passed `None`. Supply one explicit instant to all construction
stages for reproducible output. Contract metadata and constructors receive the
same `submission_time`.

## Notes

- The live source of truth is `src/xian/methods/query.py`.
- Current/raw state and indexed/history reads are intentionally separate.
- User-facing API docs should live in `xian-docs-web`; this file exists to keep
  the repo-local runtime surface understandable.
