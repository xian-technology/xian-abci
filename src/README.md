# Src

## Purpose

This folder contains the importable Python code for `xian-abci`, plus the
checked-in generated protobuf stubs that the runtime imports.

## Contents

- `xian/`: the main node package and almost all repo-owned logic
- `abci/`: lower-level ABCI server and protocol wiring
- `cometbft/` and `gogoproto/`: generated protobuf Python modules built from
  `../protos/`

## Notes

- Edit `xian/` and `abci/` directly.
- Treat the generated protobuf packages as build output. Update `../protos/`
  and rerun `../build_proto.py` instead of hand-editing `*_pb2.py`.

## Next

- Start with [`xian/README.md`](xian/README.md).
