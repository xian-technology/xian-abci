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

- `/masternodes_policy`
- `/masternodes_active`
- `/masternodes_candidates`
- `/masternodes_validator/<account>`
- `/masternodes_pending_unbonds/<account>`
- `/masternodes_open_votes/limit=<n>/offset=<n>`
- `/masternodes_vote/<proposal-id>`
- `/masternodes_vote_records/<proposal-id>`
- `/state_patch_bundles`
- `/scheduled_state_patches/<height>`

## Simulation

Readonly simulation is exposed through:

- `/simulate_tx/<encoded_payload>`

The simulator uses the latest known block metadata when available so that
`now`, `block_num`, `chain_id`, and execution mode match real execution as
closely as possible.

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

## Notes

- The live source of truth is `src/xian/methods/query.py`.
- Current/raw state and indexed/history reads are intentionally separate.
- User-facing API docs should live in `xian-docs-web`; this file exists to keep
  the repo-local runtime surface understandable.
