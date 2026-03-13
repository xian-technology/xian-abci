# Legacy Chain Assets

`xian-abci` is moving toward a universal node runtime. The committed legacy
chain bundle has been moved out of this repo and now lives in the sibling
`xian-configs` repository under `legacy/genesis/`.

## Current Source Of Truth

These assets now live in `xian-configs`:

- `legacy/genesis/*.json`
- `legacy/genesis/contracts/contracts_*.json`
- `legacy/genesis/contracts/*.s.py`

`xian-abci` may read these files through importable path helpers, but it should
not vend them as package data or treat them as local repo-owned fixtures.

## Temporary Policy

- Do not move chain-specific assets back into `xian-abci`.
- Do not add new chain-specific genesis files, seed lists, snapshots, or
  product-facing network metadata to `xian-abci`.
- Keep `mainnet`, `testnet`, `stagenet`, `devnet`, and `rcnet` assets in
  `xian-configs` only as extracted fixtures, test inputs, or historical
  references until that repo gets a normalized per-network structure.
- `genesis-rcnet.json` remains only because it is part of the same extracted
  legacy bundle. It is not a preferred network target.

## Exit Criteria

This file should become obsolete once `xian-configs` replaces the extracted
`legacy/` bundle with a normalized network-focused structure and `xian-abci`
keeps only universal presets and importable genesis helpers.
