"""A real Xian node driven through its ABCI protocol handler: a genesis built
by the genesis builder, signed transactions and the request sequence
CometBFT sends (InitChain, Info, FinalizeBlock, Commit). A node can be
stopped and started again on the same home, as after a crash."""

from __future__ import annotations

import base64
import hashlib
import shutil
from io import BytesIO
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp
from xian_accounts import Ed25519Account

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit
from cometbft.abci.v1beta2.types_pb2 import RequestInfo
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    RequestInitChain,
    Response,
)
from xian.constants import Constants
from xian.genesis_builder import build_local_network_genesis, write_genesis_block
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes

FIXTURE = Path(__file__).resolve().parents[1] / "abci_methods/fixtures/.cometbft-fixture"
CHAIN_ID = "xian-abci-node-test-1"
GENESIS_TIME = "2026-11-15T00:00:00Z"
FOUNDER = Ed25519Account("11" * 32)
VALIDATOR_SEED = bytes([7]) * 32


def validator_key(seed: bytes = VALIDATOR_SEED) -> tuple[str, dict]:
    """The validator's public key and its CometBFT private validator key."""
    public_key = bytes.fromhex(Ed25519Account(seed.hex()).public_key)
    return public_key.hex(), {
        "address": hashlib.sha256(public_key).digest()[:20].hex().upper(),
        "pub_key": {
            "type": "tendermint/PubKeyEd25519",
            "value": base64.b64encode(public_key).decode(),
        },
        "priv_key": {
            "type": "tendermint/PrivKeyEd25519",
            "value": base64.b64encode(seed + public_key).decode(),
        },
    }


def local_genesis() -> dict:
    public_key, priv_validator_key = validator_key()
    return build_local_network_genesis(
        chain_id=CHAIN_ID,
        founder_private_key="11" * 32,
        validators=[
            {
                "account_public_key": public_key,
                "name": "validator-1",
                "power": 10,
                "priv_validator_key": priv_validator_key,
            }
        ],
        genesis_time=GENESIS_TIME,
    )


def node_constants(root: Path, genesis: dict) -> type[Constants]:
    home = root / ".cometbft"
    shutil.copytree(FIXTURE, home)
    write_genesis_block(home / "config/genesis.json", genesis)

    class NodeConstants(Constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config/config.toml"
        XIAN_CONFIG = home / "config/xian.toml"
        COMETBFT_GENESIS = home / "config/genesis.json"
        STORAGE_HOME = home / "xian"

    return NodeConstants


def signed_tx(
    account: Ed25519Account,
    contract: str,
    function: str,
    kwargs: dict,
    *,
    nonce: int,
    chi: int = 50_000,
) -> bytes:
    payload = {
        "chain_id": CHAIN_ID,
        "chi_supplied": chi,
        "contract": contract,
        "function": function,
        "kwargs": kwargs,
        "nonce": nonce,
        "sender": account.public_key,
    }
    signature = account.sign_msg(canonical_json_text(payload))
    return encode_transaction_bytes(
        canonical_json_text({"metadata": {"signature": signature}, "payload": payload})
    )


class AbciNode:
    """One node home; ``start``/``stop`` open and close its application."""

    def __init__(self, constants: type[Constants]):
        self.constants = constants
        self.app = None
        self._handler = None

    async def start(self) -> "AbciNode":
        from xian.xian_abci import Xian

        self.app = await Xian.create(constants=self.constants)
        self._handler = ProtocolHandler(self.app)
        return self

    async def stop(self) -> None:
        await self.app.close()
        self.app = None
        self._handler = None

    async def _call(self, kind: str, request: Request) -> Response:
        raw = await self._handler.process(kind, request)
        return next(read_messages(BytesIO(raw), Response))

    async def init_chain(self) -> bytes:
        request = Request(init_chain=RequestInitChain(chain_id=CHAIN_ID))
        return (await self._call("init_chain", request)).init_chain.app_hash

    async def info(self):
        return (await self._call("info", Request(info=RequestInfo()))).info

    async def finalize_block(self, height: int, txs: list[bytes]):
        request = RequestFinalizeBlock(
            txs=txs,
            height=height,
            time=Timestamp(seconds=1_790_000_000 + height),
            hash=hashlib.sha256(f"block-{height}".encode()).digest(),
        )
        return (await self._call("finalize_block", Request(finalize_block=request))).finalize_block

    async def commit(self):
        return (await self._call("commit", Request(commit=RequestCommit()))).commit


def block_digest(response) -> tuple:
    """What CometBFT keeps of a FinalizeBlock response: the app hash, the
    deterministic receipt fields (LastResultsHash) and the validator updates."""
    return (
        response.app_hash,
        [(result.code, result.data, result.gas_used) for result in response.tx_results],
        [(update.pub_key.ed25519, update.power) for update in response.validator_updates],
    )
