import json
import logging
import os
import unittest
from io import BytesIO

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestQuery
from cometbft.abci.v1beta3.types_pb2 import Request, Response
from xian.constants import Constants
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)

CONTRACT_CODE = """
balances = Hash(default_value=0)


@construct
def seed(vk: str):
    balances[vk] = 100


@export
def balance_of(account: str):
    return balances[account]
""".strip()

ACCOUNT = "c93dee52d7dc6cc43af44007c3b1dae5b730ccf18a9e6fb43521f8e4064561e6"


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class TestQuery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {
            "height": 0,
            "nanos": 0,
            "chain_id": "test_chain",
        }
        self.app.client.submit(
            CONTRACT_CODE,
            name="currency",
            constructor_args={"vk": "alice"},
        )
        self.app.client.raw_driver.set(
            f"currency.balances:{ACCOUNT}",
            123.45,
        )
        self.handler = ProtocolHandler(self.app)

    async def asyncTearDown(self):
        teardown_fixtures()

    async def process_request(self, req):
        raw = await self.handler.process("query", req)
        return await deserialize(raw)

    async def test_get_query(self):
        response = await self.process_request(
            Request(
                query=RequestQuery(path=f"/get/currency.balances:{ACCOUNT}")
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "decimal")
        self.assertEqual(
            response.query.key,
            f"currency.balances:{ACCOUNT}".encode("utf-8"),
        )
        self.assertEqual(response.query.value, b"123.45")

    async def test_simulate_tx_query(self):
        payload = {
            "sender": "alice",
            "contract": "currency",
            "function": "balance_of",
            "kwargs": {"account": ACCOUNT},
        }
        encoded_payload = json.dumps(payload).encode("utf-8").hex()

        response = await self.process_request(
            Request(
                query=RequestQuery(path=f"/simulate_tx/{encoded_payload}")
            )
        )
        result = json.loads(response.query.value)

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")
        self.assertEqual(response.query.key, encoded_payload.encode("utf-8"))
        self.assertEqual(result["status"], Constants.OkCode)
        self.assertEqual(result["payload"], payload)
        self.assertEqual(result["result"], "123.45")
        self.assertEqual(
            result["state"],
            [{"key": "currency.balances:alice", "value": "99.4"}],
        )
        self.assertEqual(
            self.app.client.raw_driver.get("currency.balances:alice"),
            100,
        )

    async def test_health_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/health"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")
        self.assertEqual(response.query.key, b"")
        self.assertEqual(response.query.value, b"OK")

    async def test_get_next_nonce_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path=f"/get_next_nonce/{ACCOUNT}"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "int")
        self.assertEqual(response.query.key, ACCOUNT.encode("utf-8"))
        self.assertEqual(response.query.value, b"0")

    async def test_get_next_nonce_query_uses_pending_nonce(self):
        self.app.nonce_storage.set_pending_nonce(ACCOUNT, 9)

        response = await self.process_request(
            Request(query=RequestQuery(path=f"/get_next_nonce/{ACCOUNT}"))
        )

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.value, b"10")

    async def test_contract_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")

    async def test_contract_methods_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_methods/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")

    async def test_contract_vars_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_vars/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")

    async def test_state_patches_query_real_file(self):
        base_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        patch_file_path = os.path.join(
            base_dir,
            "src",
            "xian",
            "tools",
            "state_patches",
            "state_patches.json",
        )

        self.assertTrue(
            os.path.exists(patch_file_path),
            f"State patches file not found at {patch_file_path}",
        )

        with open(patch_file_path, "r", encoding="utf-8") as handle:
            expected_data = json.load(handle)

        self.app.block_service_mode = True
        response = await self.process_request(
            Request(query=RequestQuery(path="/state_patches"))
        )

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")

        result = json.loads(response.query.value)
        self.assertEqual(result, expected_data)
        self.assertIsInstance(result, dict)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
