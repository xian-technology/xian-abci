# Vendored Protobuf Schemas

This folder owns the exact CometBFT 0.40.0 schemas used to generate the
checked-in Python protobuf modules under `src/`.

## Contents

- `tendermint/`: the 25 `.proto` files from `proto/tendermint/` at official
  CometBFT tag `v0.40.0`, commit
  `0880b4d378f347ab16e54ec677ff50d803f37d62`, plus its Apache-2.0 license.
- `gogoproto/gogo.proto`: the pinned upstream gogoproto dependency.
- `buf.yaml` and `buf.lock`: the same release's upstream module metadata.

The historical `tendermint.*` namespace is the actual wire schema used by
CometBFT 0.40.0. The separate versioned `cometbft.*` 1.x schemas are not its
protocol: field 6 of `ConsensusParams` means `AuthorityParams` in 0.40,
whereas the 1.x schema assigns it to PBTS synchrony parameters.

## Licenses

CometBFT's schemas and generated modules retain Apache-2.0; see
`tendermint/LICENSE`. The gogoproto dependency and its generated module retain
the BSD-3-Clause license in `gogoproto/gogo.proto`.

## Workflow

When the selected upstream schemas change:

1. copy the exact `.proto` files and record the upstream release and commit
2. run `python build_proto.py` from the repo root
3. commit schemas and regenerated `src/tendermint/` and `src/gogoproto/` together
4. run `./scripts/validate-repo.sh`, including its generated-stub stale check

`build_proto.py` uses `grpcio-tools` directly when installed, or the pinned
`uvx`/`uv` tool runner. Generated `*_pb2.py` files are not hand-edited.

The wire regression corpus in `tests/unit/test_cometbft_wire_protocol.py`
contains bytes produced from the official release, including a non-empty
InitChain authority and application-mempool request variants. Binding support
does not enable optional mempool modes, validator key types or vote extensions;
the tested runtime uses the flood mempool and Ed25519 validators.

Canonical source:
https://github.com/cometbft/cometbft/tree/v0.40.0/proto/tendermint
