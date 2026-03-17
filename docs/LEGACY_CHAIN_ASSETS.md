# Chain Asset Ownership

`xian-abci` is the universal node runtime. Chain-specific assets live in the
sibling `xian-configs` repository and are no longer owned by this repo.

## Current Source Of Truth

Active network assets now live in `xian-configs`:

- `networks/<name>/manifest.json`
- `networks/<name>/genesis.json`
- `contracts/`

The old `legacy/` tree remains only as archival material. Runtime code should
prefer the canonical network and contract paths.

## Policy

- Do not add chain-specific genesis files, seed lists, snapshots, or product
  network metadata to `xian-abci`.
- Keep genesis resolution and contract loading pointed at canonical paths in
  `xian-configs`.
- Treat `legacy/` content in `xian-configs` as archive material, not a primary
  runtime input.
