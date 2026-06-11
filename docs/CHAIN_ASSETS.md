# Chain Asset Ownership

`xian-abci` is the universal node runtime. Chain-specific assets are owned by the
sibling `xian-configs` repository.

## Current Source Of Truth

Active network assets live in `xian-configs`:

- `networks/<name>/manifest.json`
- `networks/<name>/genesis.json`
- `contracts/`

## Policy

- Do not add chain-specific genesis files, seed lists, snapshots, or product
  network metadata to `xian-abci`.
- Keep genesis resolution and contract loading pointed at canonical paths in
  `xian-configs`.
