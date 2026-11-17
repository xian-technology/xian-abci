# Third-party notices

xian-abci is licensed under the MIT License (see `LICENSE`). It includes the
following third-party files, which keep their own licenses:

| Files | Origin | License |
|---|---|---|
| Portions of the ABCI server code that originate from py-abci (2017–2023, Dave Bryson and contributors; this repository's history starts there) | [davebryson/py-abci](https://github.com/davebryson/py-abci) | Apache-2.0, see `protos/tendermint/LICENSE` for the license text |
| `protos/tendermint/**` and the Python modules generated from them in `src/tendermint/` | [cometbft/cometbft](https://github.com/cometbft/cometbft), `proto/tendermint/` at tag `v0.40.0` (commit `0880b4d378f347ab16e54ec677ff50d803f37d62`) | Apache-2.0, see `protos/tendermint/LICENSE` |
| `protos/gogoproto/gogo.proto` and the Python module generated from it in `src/gogoproto/` | [gogo/protobuf](https://github.com/gogo/protobuf), Copyright (c) 2013, The GoGo Authors | BSD-3-Clause, see the license header in `protos/gogoproto/gogo.proto` |
