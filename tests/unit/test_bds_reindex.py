import base64
import json
import unittest

from xian.services.bds.reindex import BdsReindexer
from xian.utils.encoding import encode_transaction_bytes


class _FakeBlockSource:
    def __init__(self, latest_height=12):
        self._latest_height = latest_height
        self.blocks = {}
        self.block_results_map = {}

    async def latest_height(self) -> int:
        return self._latest_height

    async def block(self, height: int) -> dict:
        return self.blocks[height]

    async def block_results(self, height: int) -> dict:
        return self.block_results_map[height]

    async def close(self) -> None:
        return None


class _FakeBds:
    def __init__(self, indexed_height=None):
        self.indexed_height = indexed_height

    async def get_status(self, current_block_height=None):
        return {
            "indexed": {
                "indexed_height": self.indexed_height,
            }
        }

    async def persist_block(self, payload):
        return True


class _FakeStatePatchManager:
    def __init__(self, patch_hash=None, patches=None):
        self.patch_hash = patch_hash
        self.patches = patches or []

    def build_applied_patches_for_block(self, height):
        return self.patch_hash, self.patches


def _tx_b64(tx: dict) -> str:
    tx_json = json.dumps(tx, separators=(",", ":"))
    return base64.b64encode(encode_transaction_bytes(tx_json)).decode("utf-8")


def _tx_result_b64(tx_result: dict) -> str:
    return base64.b64encode(
        json.dumps(tx_result, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")


class BdsReindexerTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_plan_defaults_to_indexed_height_plus_one(self):
        reindexer = BdsReindexer(
            bds=_FakeBds(indexed_height=9),
            block_source=_FakeBlockSource(latest_height=12),
            state_patch_manager=_FakeStatePatchManager(),
        )

        plan = await reindexer.build_plan()

        self.assertEqual(plan.start_height, 10)
        self.assertEqual(plan.end_height, 12)
        self.assertEqual(plan.total_blocks, 3)

    async def test_build_payload_reconstructs_block_transactions(self):
        tx = {
            "payload": {
                "sender": "alice",
                "nonce": 7,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": "bob", "amount": 1},
            },
            "metadata": {"signature": "sig"},
        }
        tx_result = {
            "status": 0,
            "state": [{"key": "currency.balances:alice", "value": "99"}],
            "events": [],
            "chi_used": 7,
            "result": "ok",
        }
        source = _FakeBlockSource()
        source.blocks[12] = {
            "block_id": {"hash": "BLOCK-12"},
            "block": {
                "header": {
                    "height": "12",
                    "time": "2026-01-01T00:00:12.123456789Z",
                    "app_hash": "APP-12",
                },
                "data": {"txs": [_tx_b64(tx)]},
            },
        }
        source.block_results_map[12] = {
            "txs_results": [
                {
                    "code": "0",
                    "gas_used": "7",
                    "data": _tx_result_b64(tx_result),
                }
            ]
        }

        reindexer = BdsReindexer(
            bds=_FakeBds(indexed_height=0),
            block_source=source,
            state_patch_manager=_FakeStatePatchManager(
                patch_hash="PATCH-12",
                patches=[{"key": "x", "value": "1", "comment": "patch"}],
            ),
        )

        payload = await reindexer.build_payload(12)

        self.assertEqual(payload.block_meta["height"], 12)
        self.assertEqual(payload.block_meta["hash"], "BLOCK-12")
        self.assertEqual(payload.app_hash, "APP-12")
        self.assertEqual(payload.state_patch_hash, "PATCH-12")
        self.assertEqual(payload.transactions[0].payload["sender"], "alice")
        self.assertEqual(payload.transactions[0].tx_result["status"], 0)
        self.assertIn("hash", payload.transactions[0].tx_result)

    async def test_build_payload_skips_non_indexable_error_results(self):
        tx = {
            "payload": {
                "sender": "alice",
                "nonce": 1,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": "bob", "amount": 1},
            },
            "metadata": {"signature": "sig"},
        }
        source = _FakeBlockSource()
        source.blocks[4] = {
            "block_id": {"hash": "BLOCK-4"},
            "block": {
                "header": {
                    "height": "4",
                    "time": "2026-01-01T00:00:04Z",
                    "app_hash": "APP-4",
                },
                "data": {"txs": [_tx_b64(tx)]},
            },
        }
        source.block_results_map[4] = {
            "txs_results": [
                {
                    "code": "1",
                    "gas_used": "0",
                    "data": _tx_result_b64({"error": "decode failure"}),
                }
            ]
        }

        reindexer = BdsReindexer(
            bds=_FakeBds(indexed_height=0),
            block_source=source,
            state_patch_manager=_FakeStatePatchManager(),
        )

        payload = await reindexer.build_payload(4)

        self.assertEqual(payload.transactions, [])


if __name__ == "__main__":
    unittest.main()
