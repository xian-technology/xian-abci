"""CometBFT starts from the validator set genesis state selects.

The node computes every later validator update against the set in its state
(``validators.active_validators``). CometBFT starts from the ``validators``
of genesis.json unless InitChain returns a set, and it halts on an update
that removes a validator it does not know. So InitChain returns the set of
genesis state: a genesis.json listing other keys cannot leave CometBFT and
the node with different sets."""

from __future__ import annotations

import asyncio
import base64

from abci_node import CHAIN_ID, AbciNode, local_genesis, node_constants, validator_key

from tendermint.abci.types_pb2 import Request, RequestInitChain, ValidatorUpdate
from tendermint.crypto.keys_pb2 import PublicKey

OTHER_SEED = bytes([9]) * 32


async def _init_chain(root, genesis):
    node = await AbciNode(node_constants(root, genesis)).start()
    try:
        cometbft_validators = [
            ValidatorUpdate(
                pub_key=PublicKey(ed25519=base64.b64decode(entry["pub_key"]["value"])),
                power=int(entry["power"]),
            )
            for entry in genesis["validators"]
        ]
        init_request = node.init_request()
        init_request.validators.extend(cometbft_validators)
        request = Request(init_chain=init_request)
        return (await node._call("init_chain", request)).init_chain
    finally:
        await node.stop()


def _validator_set(response) -> dict[str, int]:
    return {update.pub_key.ed25519.hex(): update.power for update in response.validators}


def test_init_chain_returns_the_validator_set_of_genesis_state(tmp_path):
    state_key, _ = validator_key()
    response = asyncio.run(_init_chain(tmp_path, local_genesis()))
    assert _validator_set(response) == {state_key: 10}


def test_a_genesis_json_listing_other_keys_does_not_set_the_validators(tmp_path):
    state_key, _ = validator_key()
    _, other = validator_key(OTHER_SEED)
    genesis = local_genesis()
    genesis["validators"] = [
        {"address": other["address"], "pub_key": other["pub_key"], "power": "10", "name": "x"}
    ]
    response = asyncio.run(_init_chain(tmp_path, genesis))
    assert _validator_set(response) == {state_key: 10}
