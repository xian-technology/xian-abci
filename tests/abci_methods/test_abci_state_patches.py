import json
import shutil
import unittest
from io import BytesIO
from pathlib import Path

from fixtures.mock_constants import MockConstants
from google.protobuf.timestamp_pb2 import Timestamp
from utils import setup_fixtures, teardown_fixtures
from xian_runtime_types.time import Datetime

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit, RequestQuery
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    Response,
)
from xian.utils.state_patches import resolve_state_patch_dir
from xian.xian_abci import Xian

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


TARGET_CONTRACT = """
value = Variable()

@export
def get_value():
    return value.get()
"""


def governance_contract_source() -> str:
    contract_path = (
        Path(__file__).resolve().parents[3] / "xian-configs" / "contracts" / "governance.s.py"
    )
    return contract_path.read_text(encoding="utf-8")


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class GovernedStatePatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.patch_dir = resolve_state_patch_dir(MockConstants)
        if self.patch_dir.exists():
            shutil.rmtree(self.patch_dir)
        self.patch_dir.mkdir(parents=True, exist_ok=True)

        self.app = await Xian.create(constants=MockConstants)
        self.handler = ProtocolHandler(self.app)
        self.app.state_patch_manager.load_patches(self.patch_dir)

        self.app.client.submit(
            MEMBERSHIP_CONTRACT,
            name="validators",
            constructor_args={"initial_members": ["node1", "node2"]},
        )
        self.app.client.submit(
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
        self.app.client.submit(TARGET_CONTRACT, name="con_patch_target")

        self.governance = self.app.client.get_contract_proxy("governance")

    async def asyncTearDown(self):
        teardown_fixtures()

    def create_finalize_block_request(self, height: int) -> Request:
        timestamp = Timestamp()
        timestamp.seconds = height
        return Request(
            finalize_block=RequestFinalizeBlock(
                height=height,
                time=timestamp,
                txs=[],
                hash=f"block-{height}".encode("utf-8"),
            )
        )

    async def finalize_block(self, height: int):
        raw = await self.handler.process(
            "finalize_block",
            self.create_finalize_block_request(height),
        )
        response = await deserialize(raw)
        await self.handler.process("commit", Request(commit=RequestCommit()))
        return response

    async def query(self, path: str):
        raw = await self.handler.process(
            "query",
            Request(query=RequestQuery(path=path)),
        )
        return await deserialize(raw)

    def write_bundle(self, payload: dict) -> None:
        patch_id = payload["patch_id"]
        (self.patch_dir / f"{patch_id}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        self.app.state_patch_manager.load_patches(self.patch_dir)

    async def approve_patch(self, *, patch_id: str, activation_height: int):
        bundle_hash = self.app.state_patch_manager.local_bundles[patch_id].bundle_hash
        base_env = {
            "now": Datetime(2026, 1, 1),
            "block_num": 10,
            "chain_id": self.app.chain_id,
        }
        self.governance.propose_state_patch(
            patch_id=patch_id,
            bundle_hash=bundle_hash,
            activation_height=activation_height,
            summary="Apply governed patch",
            signer="node1",
            environment=base_env,
        )
        self.governance.vote(
            proposal_id=1,
            support=True,
            signer="node2",
            environment=base_env,
        )

    async def test_finalize_block_applies_governed_state_patch(self):
        self.write_bundle(
            {
                "version": 1,
                "patch_id": "patch-alpha",
                "activation_height": 12,
                "chain_id": self.app.chain_id,
                "changes": [
                    {
                        "key": "con_patch_target.value",
                        "value": "patched",
                        "comment": "patch target value",
                    }
                ],
            }
        )
        await self.approve_patch(patch_id="patch-alpha", activation_height=12)

        await self.finalize_block(11)
        self.assertIsNone(self.app.client.raw_driver.get("con_patch_target.value"))

        response = await self.finalize_block(12)
        self.assertEqual(
            self.app.client.raw_driver.get("con_patch_target.value"),
            "patched",
        )
        self.assertEqual(
            self.governance.get_patch(patch_id="patch-alpha")["status"],
            "applied",
        )
        self.assertTrue(response.finalize_block.app_hash)

    async def test_finalize_block_applies_governed_contract_source_patch(self):
        self.write_bundle(
            {
                "version": 1,
                "patch_id": "patch-source",
                "activation_height": 12,
                "changes": [
                    {
                        "key": "con_source_patch.__source__",
                        "value": (
                            "value = Variable()\n\n"
                            "@export\n"
                            "def set_value(next_value: str):\n"
                            "    value.set(next_value)\n\n"
                            "@export\n"
                            "def get_value():\n"
                            "    return value.get()\n"
                        ),
                        "comment": "deploy patched contract source",
                    }
                ],
            }
        )
        await self.approve_patch(patch_id="patch-source", activation_height=12)

        await self.finalize_block(12)
        patched = self.app.client.get_contract_proxy("con_source_patch")
        patched.set_value(next_value="hello", signer="node1")
        self.assertEqual(patched.get_value(), "hello")

    async def test_query_surfaces_local_and_scheduled_patch_inventory(self):
        self.write_bundle(
            {
                "version": 1,
                "patch_id": "patch-query",
                "activation_height": 12,
                "changes": [
                    {
                        "key": "con_patch_target.value",
                        "value": "query",
                        "comment": "query bundle",
                    }
                ],
            }
        )
        await self.approve_patch(patch_id="patch-query", activation_height=12)

        bundles_response = await self.query("/state_patch_bundles")
        bundles = json.loads(bundles_response.query.value)
        self.assertEqual(bundles[0]["patch_id"], "patch-query")

        scheduled_response = await self.query("/scheduled_state_patches/12")
        scheduled = json.loads(scheduled_response.query.value)
        self.assertEqual(scheduled[0]["patch_id"], "patch-query")
        self.assertTrue(scheduled[0]["local_bundle_available"])
