import json
import logging
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


class _FakeBDS:
    async def get_blocks(self, limit, offset):
        return [{"height": 12, "block_hash": "BLOCK-12", "tx_count": 3}]

    async def get_block(self, block_height):
        return {"height": block_height, "block_hash": f"BLOCK-{block_height}"}

    async def get_block_by_hash(self, block_hash):
        return {"height": 12, "block_hash": block_hash}

    async def get_tx(self, tx_hash):
        return {"hash": tx_hash, "block_height": 12, "sender": "alice"}

    async def get_txs_for_block(self, block_ref):
        return [{"hash": f"TX-{block_ref}", "block_height": 12, "tx_index": 0}]

    async def get_txs_by_sender(self, sender, limit, offset):
        return [{"hash": "TX-SENDER", "sender": sender}]

    async def get_txs_by_contract(self, contract, limit, offset):
        return [{"hash": "TX-CONTRACT", "contract": contract}]

    async def get_events_for_tx(self, tx_hash):
        return [{"tx_hash": tx_hash, "event": "Transfer"}]

    async def get_events(self, contract, event, limit, offset):
        return [{"contract": contract, "event": event}]

    async def get_state_patches(self, limit, offset):
        return [
            {
                "hash": "PATCH-1",
                "block_height": 12,
                "patch_count": 2,
                "patches": [
                    {
                        "key": "currency.balances:alice",
                        "value": {"__fixed__": "12.5"},
                        "comment": "repair",
                    }
                ],
            }
        ]

    async def get_state_patches_for_block(self, block_height):
        return [{"hash": f"PATCH-{block_height}", "block_height": block_height}]

    async def get_state_patch_by_hash(self, patch_hash):
        return {"hash": patch_hash, "block_height": 12, "patch_count": 2}

    async def get_state_changes_for_patch(self, patch_hash):
        return [
            {
                "key": "currency.balances:alice",
                "value": {"__fixed__": "12.5"},
                "block_height": 12,
                "write_index": 0,
            }
        ]


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
            Request(query=RequestQuery(path=f"/simulate_tx/{encoded_payload}"))
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

    async def test_state_patches_query_uses_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()
        response = await self.process_request(
            Request(query=RequestQuery(path="/state_patches"))
        )

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")

        result = json.loads(response.query.value)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["hash"], "PATCH-1")
        self.assertEqual(result[0]["block_height"], 12)

    async def test_block_and_transaction_queries_use_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()

        response = await self.process_request(
            Request(query=RequestQuery(path="/blocks/limit=5/offset=0"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)[0]["height"], 12)

        response = await self.process_request(
            Request(query=RequestQuery(path="/block/12"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)["block_hash"], "BLOCK-12"
        )

        response = await self.process_request(
            Request(query=RequestQuery(path="/block_by_hash/BLOCK-12"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)["height"], 12)

        response = await self.process_request(
            Request(query=RequestQuery(path="/tx/TX-1"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)["hash"], "TX-1")

        response = await self.process_request(
            Request(query=RequestQuery(path="/txs_for_block/12"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)[0]["hash"], "TX-12")

        response = await self.process_request(
            Request(query=RequestQuery(path="/txs_by_sender/alice"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)[0]["sender"], "alice")

        response = await self.process_request(
            Request(query=RequestQuery(path="/txs_by_contract/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["contract"], "currency"
        )

    async def test_event_queries_use_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()

        response = await self.process_request(
            Request(query=RequestQuery(path="/events_for_tx/TX-1"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["event"], "Transfer"
        )

        response = await self.process_request(
            Request(query=RequestQuery(path="/events/currency/Transfer"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["contract"], "currency"
        )

    async def test_state_patch_history_queries(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()

        response = await self.process_request(
            Request(query=RequestQuery(path="/state_patches_for_block/12"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["hash"], "PATCH-12"
        )

        response = await self.process_request(
            Request(query=RequestQuery(path="/state_patch/PATCH-1"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(json.loads(response.query.value)["hash"], "PATCH-1")

        response = await self.process_request(
            Request(query=RequestQuery(path="/state_changes_for_patch/PATCH-1"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["key"],
            "currency.balances:alice",
        )


if __name__ == "__main__":
    unittest.main()
