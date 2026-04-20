# Vendored Protobuf Schemas

This folder contains the vendored `.proto` files that `xian-abci` uses to
generate the checked-in Python protobuf modules under `src/`.

## Contents

- `cometbft/`: vendored CometBFT protobuf namespaces. This tree is an exact
  copy of `proto/cometbft/` from `cometbft/cometbft` tag `v1.0.0-alpha.2`.
- `gogoproto/gogo.proto`: upstream gogoproto dependency required by the vendored
  schemas
- `buf.yaml` and `buf.lock`: pinned upstream module metadata

## Workflow

When the vendored schemas change:

1. update the files under `protos/`
2. run `python build_proto.py` from the repo root
3. commit both the schema changes and the regenerated `src/cometbft/` and
   `src/gogoproto/` output

## Notes

- The vendored `cometbft/` tree intentionally uses the versioned
  `cometbft.*` protobuf packages. It does not come from the older
  `proto/tendermint/` layout still used by the `v0.39.x` source branch.
- `build_proto.py` uses `grpcio-tools` directly when it is installed, or falls
  back to `uvx`/`uv` tool execution
- `./scripts/validate-repo.sh` regenerates the stubs and fails if the checked-in
  generated output is stale
- treat `*_pb2.py` files as generated artifacts, not hand-edited source

Canonical upstream sources live in the CometBFT repo and Buf registry:

- https://github.com/cometbft/cometbft
- https://github.com/cometbft/cometbft/tree/v1.0.0-alpha.2/proto/cometbft
