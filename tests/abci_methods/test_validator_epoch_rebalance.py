import unittest
from io import BytesIO

from fixtures.mock_constants import MockConstants
from google.protobuf.timestamp_pb2 import Timestamp
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    Response,
)
from xian.config_paths import resolve_contracts_dir
from xian.xian_abci import Xian

NODE_1 = "11" * 32
NODE_2 = "22" * 32
NODE_3 = "33" * 32
NODE_4 = "44" * 32


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class ValidatorEpochRebalanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.handler = ProtocolHandler(self.app)
        self.driver = self.app.client.raw_driver
        contracts_dir = resolve_contracts_dir()

        contract_args = {
            "currency": {"vk": "xian-deployer"},
            "chi_cost": {"initial_rate": 20},
            "rewards": None,
            "dao": None,
        }
        for contract_file in [
            "currency.s.py",
            "dao.s.py",
            "rewards.s.py",
            "chi_cost.s.py",
        ]:
            path = contracts_dir / contract_file
            code = path.read_text(encoding="utf-8")
            name = contract_file.split(".")[0]
            self.app.client.submit(
                code,
                name=name,
                constructor_args=contract_args[name],
            )

        members_code = (contracts_dir / "validators.s.py").read_text(encoding="utf-8")
        self.app.client.submit(
            members_code,
            name="validators",
            constructor_args={
                "genesis_nodes": [NODE_1, NODE_2, NODE_3],
                "genesis_registration_fee": 1000,
            },
        )
        self.membership = self.app.client.get_contract_proxy("validators")

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

    async def test_finalize_block_runs_epoch_rebalance_without_user_transaction(
        self,
    ):
        incumbent = self.membership.active_validators.get()[0]
        challenger = NODE_4

        self.driver.set("validators.active_validators", [incumbent])
        self.driver.set("validators.candidates", [challenger])
        self.driver.set(f"validators.statuses:{incumbent}", "active")
        self.driver.set(f"validators.statuses:{challenger}", "approved")
        self.driver.set(f"validators.pending_registrations:{challenger}", False)
        self.driver.set(f"validators.pending_leave:{challenger}", False)
        self.driver.set(f"validators.self_bond:{incumbent}", 100)
        self.driver.set(f"validators.self_bond:{challenger}", 200)
        self.driver.set(f"validators.total_delegated:{incumbent}", 0)
        self.driver.set(f"validators.total_delegated:{challenger}", 0)
        self.driver.set(f"validators.delegator_lists:{incumbent}", [])
        self.driver.set(f"validators.delegator_lists:{challenger}", [])
        self.driver.set(f"validators.requested_power:{incumbent}", 10)
        self.driver.set(f"validators.requested_power:{challenger}", 21)
        self.driver.set(f"validators.powers:{incumbent}", 10)
        self.driver.set(f"validators.powers:{challenger}", 0)
        self.driver.set(f"validators.reward_keys:{incumbent}", incumbent)
        self.driver.set(f"validators.reward_keys:{challenger}", challenger)
        self.driver.set(f"validators.eligible_at_epoch:{challenger}", 1)
        self.driver.set("validators.config:selection_mode", "auto_top_n")
        self.driver.set("validators.config:max_validators", 1)
        self.driver.set("validators.config:power_mode", "requested")
        self.driver.set("validators.config:rebalance_interval", 5)
        self.driver.set("validators.config:min_self_bond", 0)
        self.driver.set("validators.config:min_total_bond", 0)
        self.driver.set("validators.last_rebalance_epoch", 0)

        before = await self.finalize_block(4)
        self.assertEqual(self.membership.active_validators.get(), [incumbent])

        after = await self.finalize_block(5)

        self.assertEqual(self.membership.active_validators.get(), [challenger])
        self.assertEqual(self.membership.statuses[challenger], "active")
        self.assertEqual(self.membership.statuses[incumbent], "approved")
        self.assertEqual(self.membership.powers[challenger], 21)
        self.assertEqual(self.driver.get("validators.last_rebalance_epoch"), 1)
        self.assertNotEqual(
            before.finalize_block.app_hash,
            after.finalize_block.app_hash,
        )
