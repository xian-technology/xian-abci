import json
import logging
import unittest
from datetime import UTC, datetime
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
    async def get_status(self, current_block_height=None):
        return {
            "worker_running": True,
            "queue_depth": 2,
            "queue_capacity": 128,
            "queue_utilization": 2 / 128,
            "spool_dir": "/tmp/xian-bds-spool",
            "spool_pending_count": 2,
            "spool_oldest_pending": {
                "block_height": 11,
                "block_hash": "BLOCK-11",
            },
            "spool_newest_pending": {
                "block_height": 12,
                "block_hash": "BLOCK-12",
            },
            "db_status": "ok",
            "db_error": None,
            "indexed": {
                "indexed_block_count": 12,
                "indexed_height": 10,
                "indexed_block_hash": "BLOCK-10",
                "indexed_block_time": datetime(
                    2026, 1, 1, 0, 0, 10, tzinfo=UTC
                ),
                "indexed_block_time_iso": "2026-01-01T00:00:10+00:00",
                "indexed_tx_count": 3,
                "indexed_app_hash": "APP-10",
            },
            "current_block_height": current_block_height,
            "height_lag": (
                current_block_height - 10
                if isinstance(current_block_height, int)
                else None
            ),
            "catching_up": True,
        }

    async def get_spool_entries(self, limit, offset):
        return [
            {
                "file": "00000000000000000011-BLOCK-11.json",
                "size_bytes": 128,
                "block_height": 11,
                "block_hash": "BLOCK-11",
                "block_time": "2026-01-01T00:00:11+00:00",
                "tx_count": 2,
                "state_patch_count": 0,
                "app_hash": "APP-11",
            }
        ]

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

    async def get_events(
        self, contract, event, limit, offset, *, after_id=None
    ):
        return [
            {
                "id": after_id + 1 if after_id is not None else 1,
                "contract": contract,
                "event": event,
            }
        ]

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

    async def test_get_query_preserves_boolean_type(self):
        self.app.client.raw_driver.set(
            f"currency.signers:{ACCOUNT}",
            True,
        )

        response = await self.process_request(
            Request(query=RequestQuery(path=f"/get/currency.signers:{ACCOUNT}"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "bool")
        self.assertEqual(
            response.query.key,
            f"currency.signers:{ACCOUNT}".encode("utf-8"),
        )
        self.assertEqual(response.query.value, b"True")

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
        self.assertEqual(response.query.info, "dict")
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

    async def test_perf_status_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/perf_status"))
        )
        payload = json.loads(response.query.value)

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")
        self.assertEqual(payload["enabled"], False)
        self.assertEqual(payload["recent_blocks"], [])

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
        source = response.query.value.decode("utf-8")
        self.assertIn("@export", source)
        self.assertNotIn("@__export", source)

    async def test_contract_code_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_code/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "str")
        code = response.query.value.decode("utf-8")
        self.assertIn("@__export('currency')", code)

    async def test_contract_methods_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_methods/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")

    async def test_contract_vars_query(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_vars/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")

    async def test_state_patches_query_uses_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()
        response = await self.process_request(
            Request(query=RequestQuery(path="/state_patches"))
        )

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "list")

        result = json.loads(response.query.value)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["hash"], "PATCH-1")
        self.assertEqual(result[0]["block_height"], 12)

    async def test_bds_status_and_spool_queries_use_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()
        self.app.current_block_meta = {"height": 12, "nanos": 0}

        response = await self.process_request(
            Request(query=RequestQuery(path="/bds_status"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        status = json.loads(response.query.value)
        self.assertTrue(status["worker_running"])
        self.assertEqual(status["spool_pending_count"], 2)
        self.assertEqual(status["height_lag"], 2)
        self.assertEqual(status["indexed"]["indexed_height"], 10)
        self.assertEqual(
            status["indexed"]["indexed_block_time"],
            "2026-01-01T00:00:10+00:00",
        )

        response = await self.process_request(
            Request(query=RequestQuery(path="/bds_spool/limit=10/offset=0"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        spool_entries = json.loads(response.query.value)
        self.assertEqual(spool_entries[0]["block_height"], 11)
        self.assertEqual(spool_entries[0]["tx_count"], 2)

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

        response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/events/currency/Transfer/after_id=41/limit=10"
                )
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertEqual(payload[0]["id"], 42)
        self.assertEqual(payload[0]["event"], "Transfer")

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
