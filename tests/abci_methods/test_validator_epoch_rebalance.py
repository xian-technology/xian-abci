import unittest
from io import BytesIO

from fixtures.mock_constants import MockConstants
from google.protobuf.timestamp_pb2 import Timestamp
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
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

        members_code = (contracts_dir / "members.s.py").read_text(encoding="utf-8")
        self.app.client.submit(
            members_code,
            name="masternodes",
            constructor_args={
                "genesis_nodes": [NODE_1, NODE_2, NODE_3],
                "genesis_registration_fee": 1000,
            },
        )
        self.membership = self.app.client.get_contract("masternodes")

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
        return await deserialize(raw)

    async def test_finalize_block_runs_epoch_rebalance_without_user_transaction(
        self,
    ):
        incumbent = self.membership.nodes.get()[0]
        challenger = NODE_4

        self.driver.set("masternodes.nodes", [incumbent])
        self.driver.set("masternodes.candidates", [challenger])
        self.driver.set(f"masternodes.statuses:{incumbent}", "active")
        self.driver.set(f"masternodes.statuses:{challenger}", "approved")
        self.driver.set(f"masternodes.pending_registrations:{challenger}", False)
        self.driver.set(f"masternodes.pending_leave:{challenger}", False)
        self.driver.set(f"masternodes.self_bond:{incumbent}", 100)
        self.driver.set(f"masternodes.self_bond:{challenger}", 200)
        self.driver.set(f"masternodes.total_delegated:{incumbent}", 0)
        self.driver.set(f"masternodes.total_delegated:{challenger}", 0)
        self.driver.set(f"masternodes.delegator_lists:{incumbent}", [])
        self.driver.set(f"masternodes.delegator_lists:{challenger}", [])
        self.driver.set(f"masternodes.requested_power:{incumbent}", 10)
        self.driver.set(f"masternodes.requested_power:{challenger}", 21)
        self.driver.set(f"masternodes.validator_power:{incumbent}", 10)
        self.driver.set(f"masternodes.validator_power:{challenger}", 0)
        self.driver.set(f"masternodes.reward_keys:{incumbent}", incumbent)
        self.driver.set(f"masternodes.reward_keys:{challenger}", challenger)
        self.driver.set(f"masternodes.eligible_at_epoch:{challenger}", 1)
        self.driver.set("masternodes.config:selection_mode", "auto_top_n")
        self.driver.set("masternodes.config:max_validators", 1)
        self.driver.set("masternodes.config:power_mode", "requested")
        self.driver.set("masternodes.config:rebalance_interval", 5)
        self.driver.set("masternodes.config:min_self_bond", 0)
        self.driver.set("masternodes.config:min_total_bond", 0)
        self.driver.set("masternodes.last_rebalance_epoch", 0)

        before = await self.finalize_block(4)
        self.assertEqual(self.membership.nodes.get(), [incumbent])

        after = await self.finalize_block(5)

        self.assertEqual(self.membership.nodes.get(), [challenger])
        self.assertEqual(self.membership.statuses[challenger], "active")
        self.assertEqual(self.membership.statuses[incumbent], "approved")
        self.assertEqual(self.membership.validator_power[challenger], 21)
        self.assertEqual(self.driver.get("masternodes.last_rebalance_epoch"), 1)
        self.assertNotEqual(
            before.finalize_block.app_hash,
            after.finalize_block.app_hash,
        )
