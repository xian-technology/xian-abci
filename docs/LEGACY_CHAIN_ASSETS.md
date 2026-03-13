# Legacy Chain Assets

`xian-abci` is moving toward a universal node runtime. Until the future
`xian-configs` repo exists, this repo still carries a temporary bundle of
chain-specific fixtures under `src/xian/tools/genesis/`.

## Temporary Keep List

Keep these files in place for now:

- `src/xian/tools/genesis/*.json`
  Reason: existing exported genesis fixtures and local presets still need to be
  available for smoke paths, fixture inspection, and later extraction.
- `src/xian/tools/genesis/contracts/contracts_*.json`
  Reason: these manifests describe how the legacy genesis fixtures were built.
- `src/xian/tools/genesis/contracts/*.s.py`
  Reason: the new importable `xian.genesis_builder` still reads these contract
  sources while the bundle remains in this repo.

## Temporary Policy

- Do not add new chain-specific genesis files, seed lists, snapshots, or
  product-facing network metadata here.
- Do not make operator docs point at these files as the long-term source of
  truth.
- Keep `mainnet`, `testnet`, `stagenet`, `devnet`, and `rcnet` assets only as
  extraction inputs, test fixtures, or historical references.
- `genesis-rcnet.json` stays only because it is part of the same legacy bundle.
  It is not a preferred network target and should move or disappear together
  with the rest of the bundle.

## Exit Criteria

This file should become obsolete once:

1. `xian-configs` exists under `xian-technology`
2. the committed genesis bundle moves out of `xian-abci`
3. `xian-abci` keeps only universal presets and importable genesis helpers
