import json
import logging
import unittest
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch

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

MASTERNODES_CODE = """
policy = Variable()
active = Variable()
candidates = Variable()
validators = Hash(default_value=None)
pending_unbond_owner_ids = Hash(default_value=None)
pending_unbonds = Hash(default_value=None)


@construct
def seed():
    policy.set({"selection_mode": "manual", "max_validators": 5})
    active.set([{"account": "alice", "status": "active"}])
    candidates.set([{"account": "candidate-1", "status": "approved"}])
    validators["alice"] = {
        "account": "alice",
        "status": "active",
        "total_bond": 150,
    }
    pending_unbond_owner_ids["alice"] = [4]
    pending_unbonds[4] = {
        "owner": "alice",
        "amount": 25,
    }


@export
def get_policy_config():
    return policy.get()


@export
def get_active_validators():
    return active.get()


@export
def get_pending_candidates():
    return candidates.get()


@export
def get_validator(account: str):
    return validators[account]


@export
def get_pending_unbond_ids(owner: str):
    return pending_unbond_owner_ids[owner] or []


@export
def get_pending_unbond(unbond_id: int):
    return pending_unbonds[unbond_id]
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

    async def get_recent_addresses(self, limit, offset):
        return [
            {
                "address": "alice",
                "tx_count": 5,
                "last_block_height": 12,
                "last_tx_hash": "TX-ADDR",
            }
        ]

    async def get_txs_by_contract(self, contract, limit, offset):
        return [{"hash": "TX-CONTRACT", "contract": contract}]

    async def get_events_for_tx(self, tx_hash):
        return [{"tx_hash": tx_hash, "event": "Transfer"}]

    async def get_shielded_output_tags(
        self, tag_value, limit, offset, *, kind="sync_hint", after_id=None
    ):
        return [
            {
                "id": after_id + 1 if after_id is not None else 1,
                "tag_kind": kind,
                "tag_value": tag_value,
                "commitment": "0x" + "11" * 32,
            }
        ]

    async def get_shielded_wallet_history(
        self, tag_value, limit, after_note_index, *, kind="sync_hint"
    ):
        return [
            {
                "event_id": 41,
                "tag_kind": kind,
                "tag_value": tag_value,
                "note_index": after_note_index,
                "commitment": "0x" + "22" * 32,
                "output_payload": "0x1234",
            }
        ]

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

    async def get_recent_events(self, limit, offset):
        return [
            {
                "id": 8,
                "contract": "currency",
                "event": "Transfer",
                "tx_hash": "TX-RECENT",
            }
        ]

    async def get_token_balances(
        self, address, limit, offset, *, include_zero=False
    ):
        return {
            "available": True,
            "address": address,
            "items": [
                {
                    "contract": "currency",
                    "balance": "42",
                    "name": "Xian",
                    "symbol": "XIAN",
                    "logo_url": "https://example.com/xian.svg",
                    "last_tx_hash": "TX-BALANCE",
                    "last_block_height": 12,
                    "updated_at": "2026-01-01T00:00:12+00:00",
                }
            ],
            "total": 1,
            "limit": limit,
            "offset": offset,
            "include_zero": include_zero,
        }

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

    async def get_developer_rewards(self, recipient_key):
        return {
            "recipient_key": recipient_key,
            "total_rewards": "42.5",
            "reward_count": 6,
            "tx_count": 4,
            "contract_count": 3,
            "first_block_height": 7,
            "last_block_height": 12,
            "first_reward_at": "2026-01-01T00:00:07+00:00",
            "last_reward_at": "2026-01-01T00:00:12+00:00",
        }

    async def get_contract_summary(self, contract_name):
        return {
            "name": contract_name,
            "last_tx_hash": "TX-CONTRACT-CREATE",
            "submitted_at_block": 12,
            "submitted_at": "2026-01-01T00:00:12+00:00",
            "creator": "alice",
            "tx_count": 5,
            "total_rewards": "18.25",
            "reward_count": 3,
            "first_block_height": 12,
            "last_block_height": 18,
            "first_reward_at": "2026-01-01T00:00:12+00:00",
            "last_reward_at": "2026-01-01T00:00:18+00:00",
        }


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
        self.app.client.submit(
            MASTERNODES_CODE,
            name="masternodes",
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

    async def test_simulate_tx_query_returns_structured_failure_when_disabled(
        self,
    ):
        self.app.simulator.enabled = False
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
        self.assertEqual(result["status"], Constants.ErrorCode)
        self.assertIn("disabled", result["result"])

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
        self.assertFalse(payload["parallel_execution_enabled"])
        self.assertEqual(payload["parallel_execution_workers"], 0)
        self.assertEqual(payload["parallel_execution_min_transactions"], 8)

    async def test_masternodes_dashboard_queries(self):
        self.app.client.raw_driver.set("masternodes.total_votes", 2)
        self.app.client.raw_driver.set(
            "masternodes.votes:1",
            {
                "type": "update_policy",
                "status": "pending",
                "finalized": False,
            },
        )
        self.app.client.raw_driver.set(
            "masternodes.votes:2",
            {
                "type": "jail_member",
                "status": "approved",
                "finalized": True,
            },
        )

        policy_response = await self.process_request(
            Request(query=RequestQuery(path="/masternodes_policy"))
        )
        active_response = await self.process_request(
            Request(query=RequestQuery(path="/masternodes_active"))
        )
        validator_response = await self.process_request(
            Request(query=RequestQuery(path="/masternodes_validator/alice"))
        )
        unbonds_response = await self.process_request(
            Request(
                query=RequestQuery(path="/masternodes_pending_unbonds/alice")
            )
        )
        votes_response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/masternodes_open_votes/limit=25/offset=0"
                )
            )
        )

        self.assertEqual(policy_response.query.info, "dict")
        self.assertEqual(
            json.loads(policy_response.query.value)["selection_mode"],
            "manual",
        )
        self.assertEqual(active_response.query.info, "list")
        self.assertEqual(
            json.loads(active_response.query.value)[0]["account"],
            "alice",
        )
        self.assertEqual(validator_response.query.info, "dict")
        self.assertEqual(
            json.loads(validator_response.query.value)["total_bond"],
            150,
        )
        self.assertEqual(unbonds_response.query.info, "list")
        self.assertEqual(
            json.loads(unbonds_response.query.value)[0]["unbond_id"],
            4,
        )
        self.assertEqual(votes_response.query.info, "list")
        self.assertEqual(
            json.loads(votes_response.query.value),
            [
                {
                    "proposal_id": 1,
                    "type": "update_policy",
                    "status": "pending",
                    "finalized": False,
                }
            ],
        )

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

    async def test_contract_info_query_returns_runtime_metadata(self):
        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_info/currency"))
        )

        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")
        payload = json.loads(response.query.value)
        self.assertEqual(payload["name"], "currency")
        self.assertEqual(payload["developer"], "sys")
        self.assertTrue(payload["has_source"])
        self.assertIn("submitted_at", payload)

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

    async def test_bds_status_falls_back_to_latest_committed_height(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()
        self.app.current_block_meta = None

        with patch(
            "xian.methods.query.get_latest_block_height", return_value=14
        ):
            response = await self.process_request(
                Request(query=RequestQuery(path="/bds_status"))
            )

        self.assertEqual(response.query.code, Constants.OkCode)
        status = json.loads(response.query.value)
        self.assertEqual(status["current_block_height"], 14)
        self.assertEqual(status["height_lag"], 4)

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
            Request(query=RequestQuery(path="/addresses/limit=10/offset=0"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["items"][0]["address"], "alice")

        response = await self.process_request(
            Request(query=RequestQuery(path="/txs_by_contract/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(
            json.loads(response.query.value)[0]["contract"], "currency"
        )

        response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/token_balances/alice/limit=10/offset=0/include_zero=true"
                )
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["address"], "alice")
        self.assertEqual(payload["items"][0]["contract"], "currency")
        self.assertEqual(payload["items"][0]["balance"], "42")

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

        response = await self.process_request(
            Request(query=RequestQuery(path="/recent_events/limit=25"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["items"][0]["tx_hash"], "TX-RECENT")

        response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/shielded_output_tags/0x1234/limit=25/offset=5"
                )
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["limit"], 25)
        self.assertEqual(payload["offset"], 5)
        self.assertEqual(payload["items"][0]["tag_kind"], "sync_hint")
        self.assertEqual(payload["items"][0]["tag_value"], "0x1234")

        response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/shielded_wallet_history/0x1234/limit=25/after_note_index=5"
                )
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["limit"], 25)
        self.assertEqual(payload["after_note_index"], 5)
        self.assertEqual(payload["items"][0]["tag_kind"], "sync_hint")
        self.assertEqual(payload["items"][0]["note_index"], 5)

    async def test_contract_listing_query_is_available_without_bds(self):
        self.app.client.submit(
            CONTRACT_CODE,
            name="con_token_b",
            constructor_args={"vk": "bob"},
        )

        response = await self.process_request(
            Request(
                query=RequestQuery(
                    path="/contracts/limit=10/offset=0/sort=name/order=asc"
                )
            )
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        payload = json.loads(response.query.value)
        contract_names = [item["name"] for item in payload["items"]]
        self.assertIn("currency", contract_names)
        self.assertIn("con_token_b", contract_names)
        self.assertEqual(payload["sort"], "name")
        self.assertEqual(payload["order"], "asc")

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

    async def test_developer_rewards_query_uses_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()

        response = await self.process_request(
            Request(query=RequestQuery(path=f"/developer_rewards/{ACCOUNT}"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")
        payload = json.loads(response.query.value)
        self.assertEqual(payload["recipient_key"], ACCOUNT)
        self.assertEqual(payload["total_rewards"], "42.5")
        self.assertEqual(payload["tx_count"], 4)

    async def test_contract_summary_query_uses_bds(self):
        self.app.block_service_mode = True
        self.app.bds = _FakeBDS()

        response = await self.process_request(
            Request(query=RequestQuery(path="/contract_summary/currency"))
        )
        self.assertEqual(response.query.code, Constants.OkCode)
        self.assertEqual(response.query.info, "dict")
        payload = json.loads(response.query.value)
        self.assertEqual(payload["name"], "currency")
        self.assertEqual(payload["creator"], "alice")
        self.assertEqual(payload["total_rewards"], "18.25")


if __name__ == "__main__":
    unittest.main()
