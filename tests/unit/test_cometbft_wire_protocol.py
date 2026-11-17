"""Byte fixtures produced by unmodified CometBFT v0.40.0 protobuf schemas."""

import asyncio
from io import BytesIO

import pytest

from abci.server import ProtocolHandler
from abci.utils import read_messages
from tendermint.abci.types_pb2 import Request, Response

# Generated from v0.40.0/0880b4d proto/tendermint/abci/types.proto, not from
# the application's descriptors. Authority is field6, distinct from PBTS.
INIT_CHAIN_AUTHORITY = bytes.fromhex(
    "2a280a021001120d7869616e2d776972652d3034301a1332110a0f7869616e2d676f7665726e616e6365"
)


def test_official_init_chain_preserves_authority_and_exact_timestamp():
    request = Request.FromString(INIT_CHAIN_AUTHORITY)
    assert request.WhichOneof("value") == "init_chain"
    assert request.init_chain.chain_id == "xian-wire-040"
    assert request.init_chain.time.ToNanoseconds() == 1
    assert request.init_chain.consensus_params.authority.authority == "xian-governance"
    assert request.SerializeToString() == INIT_CHAIN_AUTHORITY


@pytest.mark.parametrize(
    "wire,kind",
    [
        ("aa010c0a0a776972652d70726f6265", "insert_tx"),
        ("b20105086410c801", "reap_txs"),
    ],
)
def test_unsupported_app_mempool_requests_return_protocol_errors(wire, kind):
    request = Request.FromString(bytes.fromhex(wire))
    assert request.WhichOneof("value") == kind
    raw = asyncio.run(ProtocolHandler(object()).process(kind, request))
    response = next(read_messages(BytesIO(raw), Response))
    assert response.WhichOneof("value") == "exception"
    assert response.exception.error == "ABCI request not found"
