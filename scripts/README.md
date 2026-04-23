# Scripts

## Purpose

This folder contains the deliberately small script surface that sits outside the
importable runtime code.

## Contents

- `validate-release.sh`: release-grade validation wrapper for the repo
- `validate-repo.sh`: the preferred full validation entrypoint for local and CI
  use
- `benchmark_shielded_chi.py`: manual shielded-fee benchmark harness for the
  shielded note-token path

## Notes

- Keep this folder deliberately small.
- Prefer importable helpers in `src/xian/` for reusable logic, and keep scripts
  here only when they are truly repo-level maintenance entrypoints.
- Protobuf generation does not live here. Use the repo-root `build_proto.py`
  entrypoint instead.
- `benchmark_shielded_chi.py` is a manual benchmark, not part of the default CI
  or `make validate` path, and it expects sibling `xian-contracting` and
  `xian-contracts` checkouts.
