# abci

## Purpose

This package is the low-level ABCI server layer: the asyncio TCP server and
wire-protocol plumbing that connects CometBFT to the Xian application in
`../xian/`.

## Contents

- `server.py` — asyncio TCP server that speaks the ABCI socket protocol with
  CometBFT, frames protobuf messages, and dispatches requests to the
  application.
- `utils.py` — shared helpers for the server layer.

## Notes

- This layer is protocol glue. Application behavior (transaction execution,
  queries, consensus state) belongs in `../xian/`, not here.
- The protobuf types come from the generated `cometbft.*` stubs under
  `../cometbft/`; regenerate them through the repo-root `build_proto.py`
  instead of editing imports here ad hoc.

## Next

- See [`../xian/README.md`](../xian/README.md) for the application that this
  server hosts.
