# Governance Integration

This file is runtime-centric. It documents how `xian-abci` integrates with the
current governance contracts; it is not a full end-user governance manual.

## Governance Layers

Current Xian runtime behavior spans two related governance layers:

- `masternodes`: membership and validator governance used for validator set,
  candidate, unbond, and vote state
- `governance`: protocol-governance contract used for governed contract calls
  and scheduled state-patch bundles

Other contracts such as `dao`, `rewards`, and `chi_cost` still participate in
network policy, but the runtime integration points in this repo center on
membership state, validator power, rewards distribution, and governed state
patch execution.

## Where `xian-abci` Integrates

Validator and membership integration:

- `src/xian/validators.py` reads validator membership and power from
  `masternodes`
- `src/xian/rewards.py` reads reward-related membership state from
  `masternodes`
- `src/xian/methods/query.py` exposes runtime queries such as:
  - `/masternodes_policy`
  - `/masternodes_active`
  - `/masternodes_candidates`
  - `/masternodes_validator/<account>`
  - `/masternodes_pending_unbonds/<account>`
  - `/masternodes_open_votes/limit=<n>/offset=<n>`
  - `/masternodes_vote/<proposal-id>`
  - `/masternodes_vote_records/<proposal-id>`

Protocol-governance integration:

- `src/xian/utils/state_patches.py` loads local patch bundles and matches them
  against approved governance state
- `src/xian/methods/finalize_block.py` applies approved scheduled patches at the
  configured activation height
- `src/xian/methods/query.py` exposes:
  - `/state_patch_bundles`
  - `/scheduled_state_patches/<height>`
- executed patches are fingerprinted into block execution and persisted to BDS

## Source Of Truth

The authoritative contract semantics do not live in this docs folder.

- canonical contract sources live in `xian-configs/contracts/`
- runtime integration lives in `src/xian/`
- coverage lives in:
  - `tests/governance/test_governance.py`
  - `tests/governance/test_protocol_governance.py`
  - `tests/abci_methods/test_abci_state_patches.py`

## Notes

- Keep user-facing governance documentation in `xian-docs-web`.
- Keep this file focused on runtime-owned integration points and query surfaces.
