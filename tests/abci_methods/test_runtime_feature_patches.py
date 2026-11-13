"""Switching the zk runtime feature on a live chain with governed state patches.

Two nodes receive the same blocks. One keeps running; the other restarts after
every patch activation. Both must produce the same block results at every
height, and every node-side copy of the features must follow committed state.
"""

import asyncio
import json
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from nacl.signing import SigningKey
from xian_runtime_types.time import Datetime

from cometbft.abci.v1beta3.types_pb2 import RequestFinalizeBlock
from contracting.runtime_features import get_driver_runtime_features, runtime_feature_state_key
from xian.constants import Constants
from xian.state_root import StateRootCache
from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
from xian.utils.state_patches import resolve_state_patch_dir
from xian.xian_abci import Xian

SIGNING_KEY = SigningKey(bytes(range(32)))
SENDER = SIGNING_KEY.verify_key.encode().hex()
ZK_FEATURE_KEY = runtime_feature_state_key("zk")
FIXTURE = Path(__file__).parent / "fixtures/.cometbft-fixture"

MEMBERSHIP_CONTRACT = """
members = Variable()

@construct
def seed(initial_members: list):
    members.set(initial_members)

@export
def get_members():
    return members.get()

@export
def is_member(account: str):
    return account in members.get()
"""

PLAIN_CONTRACT = "@export\ndef ping():\n    return 1\n"
ZK_CONTRACT = "@export\ndef check():\n    return zk.is_available()\n"


def governance_contract_source() -> str:
    path = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts" / "governance.s.py"
    return path.read_text(encoding="utf-8")


def node_constants(home: Path) -> type[Constants]:
    class NodeConstants(Constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config/config.toml"
        XIAN_CONFIG = home / "config/xian.toml"
        COMETBFT_GENESIS = home / "config/genesis.json"
        STORAGE_HOME = home / "xian"

    return NodeConstants


def zk_patch_bundle(patch_id: str, height: int, enabled: bool) -> dict:
    return {
        "version": 1,
        "patch_id": patch_id,
        "activation_height": height,
        "changes": [{"key": ZK_FEATURE_KEY, "value": enabled}],
    }


class Node:
    """A node home driven through the ABCI app, restartable in place."""

    def __init__(self, home: Path, cleanup: AsyncExitStack):
        shutil.copytree(FIXTURE, home)
        self.constants = node_constants(home)
        self.patch_dir = resolve_state_patch_dir(self.constants)
        self.patch_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup = cleanup
        self.app = None

    async def start(self) -> None:
        self.app = await Xian.create(constants=self.constants)
        app = self.app
        self.cleanup.push_async_callback(app.close)
        self.cleanup.callback(app.client.close)

    async def restart(self) -> None:
        await self.app.close()
        self.app.client.close()
        await self.start()

    def setup_chain(self) -> None:
        client = self.app.client
        client.submit(
            MEMBERSHIP_CONTRACT,
            name="validators",
            constructor_args={"initial_members": ["node1", "node2"]},
        )
        client.submit(
            governance_contract_source(),
            name="governance",
            constructor_args={
                "membership_contract_name": "validators",
                "approval_threshold_numerator": 1,
                "approval_threshold_denominator": 1,
                "min_patch_delay_blocks": 2,
                "emergency_patch_delay_blocks": 1,
            },
        )
        client.submit(PLAIN_CONTRACT, name="con_plain", owner=None)
        client.raw_driver.set(f"currency.balances:{SENDER}", 10_000_000)
        client.raw_driver.set("chi_cost.S:value", 20)
        # Local submission stamps the wall clock; both nodes need the same state.
        client.raw_driver.set("submission.__submitted__", Datetime(2026, 1, 1))
        client.raw_driver.commit()
        self.app.state_root_cache = StateRootCache.from_driver(client.raw_driver)

    def schedule(self, bundle: dict, *, proposal_id: int, block_num: int) -> None:
        path = self.patch_dir / f"{bundle['patch_id']}.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        manager = self.app.state_patch_manager
        manager.load_patches(self.patch_dir)
        governance = self.app.client.get_contract_proxy("governance")
        environment = {
            "now": Datetime(2026, 1, 1),
            "block_num": block_num,
            "chain_id": self.app.chain_id,
        }
        governance.propose_state_patch(
            patch_id=bundle["patch_id"],
            bundle_hash=manager.local_bundles[bundle["patch_id"]].bundle_hash,
            activation_height=bundle["activation_height"],
            summary="Switch the zk runtime feature",
            signer="node1",
            environment=environment,
        )
        governance.vote(proposal_id=proposal_id, support=True, signer="node2", environment=environment)

    async def finalize(self, height: int, txs: list[bytes]):
        response = await self.app.finalize_block(
            RequestFinalizeBlock(height=height, hash=height.to_bytes(32, "big"), txs=txs)
        )
        await self.app.commit()
        return response

    def feature_copies(self) -> dict:
        app = self.app
        return {
            "committed": app.client.raw_driver.value_from_disk(ZK_FEATURE_KEY),
            "node": app.runtime_features,
            "simulator": app.simulator.runtime_features,
            "driver": get_driver_runtime_features(app.client.raw_driver),
        }


class Chain:
    """The same blocks on a running node and on a node restarted after each patch."""

    def __init__(self, running: Node, restarted: Node):
        self.nodes = (running, restarted)
        self.running, self.restarted = running, restarted
        self.height = 0
        self.nonce = 0
        self.proposals = 0

    def tx(self, contract: str, function: str, kwargs: dict | None = None) -> bytes:
        payload = {
            "sender": SENDER,
            "contract": contract,
            "function": function,
            "kwargs": kwargs or {},
            "nonce": self.nonce,
            "chi_supplied": 100_000,
            "chain_id": self.running.app.chain_id,
        }
        self.nonce += 1
        signature = SIGNING_KEY.sign(canonical_json_text(payload).encode()).signature.hex()
        envelope = {"payload": payload, "metadata": {"signature": signature}}
        return encode_transaction_bytes(canonical_json_text(envelope))

    def deploy(self, name: str, code: str) -> bytes:
        return self.tx("submission", "submit_contract", {"name": name, "code": code})

    async def block(self, *txs: bytes) -> list[dict]:
        self.height += 1
        responses = [await node.finalize(self.height, list(txs)) for node in self.nodes]
        running, restarted = responses
        assert running.app_hash == restarted.app_hash
        assert [r.SerializeToString() for r in running.tx_results] == [
            r.SerializeToString() for r in restarted.tx_results
        ]
        return [
            {"code": result.code, **json.loads(result.data)} for result in running.tx_results
        ]

    async def switch_zk(self, enabled: bool) -> None:
        """Schedule a patch, run blocks up to its activation, restart one node."""
        self.proposals += 1
        activation = self.height + 2
        bundle = zk_patch_bundle(f"zk-switch-{self.proposals}", activation, enabled)
        for node in self.nodes:
            node.schedule(bundle, proposal_id=self.proposals, block_num=self.height)
        await self.block()
        # The activation block's own transactions still run under the old value.
        [activation_call] = await self.block(self.tx("con_plain", "ping"))
        assert activation_call["code"] == 0
        await self.restarted.restart()
        running, restarted = (node.feature_copies() for node in self.nodes)
        assert running == restarted
        assert running["committed"] is enabled
        assert running["node"] == running["simulator"] == running["driver"] == {"zk": enabled}


def assert_zk_disabled_failure(receipt: dict) -> None:
    assert receipt["code"] == 1
    assert "zk runtime feature is disabled" in receipt["result"]
    # A normal failed receipt: the transaction pays for the chi it used.
    assert receipt["chi_used"] > 0


def test_zk_switches_on_off_and_on_again_identically_on_running_and_restarted_nodes(tmp_path):
    pytest.importorskip("xian_vm_core")
    asyncio.run(switch_zk_on_off_and_on_again(tmp_path))


async def switch_zk_on_off_and_on_again(tmp_path: Path) -> None:
    async with AsyncExitStack() as cleanup:
        nodes = [Node(tmp_path / name, cleanup) for name in ("running", "restarted")]
        for node in nodes:
            await node.start()
            node.setup_chain()
        chain = Chain(*nodes)
        # This test is about the chain feature; the node's verifier is present.
        cleanup.enter_context(patch("contracting.stdlib.bridge.zk.is_available", return_value=True))

        deploy, ping = await chain.block(
            chain.deploy("con_zk_probe", ZK_CONTRACT), chain.tx("con_plain", "ping")
        )
        assert_zk_disabled_failure(deploy)
        assert ping["code"] == 0

        await chain.switch_zk(True)
        deploy, call = await chain.block(
            chain.deploy("con_zk_probe", ZK_CONTRACT), chain.tx("con_zk_probe", "check")
        )
        assert (deploy["code"], call["code"]) == (0, 0)

        await chain.switch_zk(False)
        call, ping, deploy = await chain.block(
            chain.tx("con_zk_probe", "check"),
            chain.tx("con_plain", "ping"),
            chain.deploy("con_zk_other", ZK_CONTRACT),
        )
        assert_zk_disabled_failure(call)
        assert ping["code"] == 0
        assert_zk_disabled_failure(deploy)
        simulated = await chain.running.app.simulator.simulate_encoded_transaction(
            json.dumps(
                {"sender": SENDER, "contract": "con_zk_probe", "function": "check", "kwargs": {}}
            )
            .encode()
            .hex()
        )
        assert simulated["status"] == 1
        assert "zk runtime feature is disabled" in str(simulated["result"])

        await chain.switch_zk(True)
        [call] = await chain.block(chain.tx("con_zk_probe", "check"))
        assert call["code"] == 0
