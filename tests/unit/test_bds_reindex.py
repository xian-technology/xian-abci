import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from xian.services.bds.reindex import (
    BdsReindexer,
    CometBftRpcClient,
    ReindexPlan,
    datetime_to_nanos,
    parse_rfc3339_nano,
    run_bds_reindex,
)
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

    async def test_build_payload_rejects_source_hash_mismatch_against_trusted_block(self):
        source = _FakeBlockSource()
        source.blocks[12] = {
            "block_id": {"hash": "BLOCK-12"},
            "block": {
                "header": {
                    "height": "12",
                    "time": "2026-01-01T00:00:12Z",
                    "app_hash": "APP-12",
                },
                "data": {"txs": []},
            },
        }
        source.block_results_map[12] = {"txs_results": []}

        trusted = _FakeBlockSource()
        trusted.blocks[12] = {
            "block_id": {"hash": "BLOCK-999"},
            "block": {
                "header": {
                    "height": "12",
                    "time": "2026-01-01T00:00:12Z",
                    "app_hash": "APP-12",
                },
                "data": {"txs": []},
            },
        }

        reindexer = BdsReindexer(
            bds=_FakeBds(indexed_height=0),
            block_source=source,
            state_patch_manager=_FakeStatePatchManager(),
            trusted_block_source=trusted,
        )

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            await reindexer.build_payload(12)

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

    async def test_build_payload_rejects_tx_result_count_mismatch(self):
        source = _FakeBlockSource()
        source.blocks[5] = {
            "block_id": {"hash": "BLOCK-5"},
            "block": {
                "header": {
                    "height": "5",
                    "time": "2026-01-01T00:00:05Z",
                    "app_hash": "APP-5",
                },
                "data": {"txs": [_tx_b64({"payload": {}, "metadata": {}})]},
            },
        }
        source.block_results_map[5] = {"txs_results": []}

        reindexer = BdsReindexer(
            bds=_FakeBds(indexed_height=0),
            block_source=source,
            state_patch_manager=_FakeStatePatchManager(),
        )

        with self.assertRaisesRegex(ValueError, "tx/result count mismatch"):
            await reindexer.build_payload(5)

    async def test_verify_rejects_app_hash_mismatch_against_trusted_block(self):
        block = {
            "block_id": {"hash": "BLOCK-6"},
            "block": {"header": {"app_hash": "APP-SOURCE"}},
        }
        trusted = _FakeBlockSource()
        trusted.blocks[6] = {
            "block_id": {"hash": "BLOCK-6"},
            "block": {"header": {"app_hash": "APP-TRUSTED"}},
        }
        reindexer = BdsReindexer(
            bds=_FakeBds(),
            block_source=_FakeBlockSource(),
            state_patch_manager=_FakeStatePatchManager(),
            trusted_block_source=trusted,
        )

        with self.assertRaisesRegex(ValueError, "app hash mismatch"):
            await reindexer._verify_block_against_trusted_source(
                height=6,
                block_response=block,
            )

    async def test_reindex_range_counts_persisted_blocks(self):
        class _SelectiveBds(_FakeBds):
            def __init__(self):
                super().__init__(indexed_height=0)
                self.heights = []

            async def persist_block(self, payload):
                self.heights.append(payload)
                return len(self.heights) % 2 == 1

        bds = _SelectiveBds()
        reindexer = BdsReindexer(
            bds=bds,
            block_source=_FakeBlockSource(),
            state_patch_manager=_FakeStatePatchManager(),
        )

        async def _payload(height):
            return {"height": height}

        with mock.patch.object(reindexer, "build_payload", side_effect=_payload):
            persisted = await reindexer.reindex_range(
                ReindexPlan(
                    latest_height=4,
                    indexed_height=0,
                    start_height=1,
                    end_height=4,
                )
            )

        self.assertEqual(persisted, 2)
        self.assertEqual(len(bds.heights), 4)

    def test_extract_block_txs_handles_all_shapes(self):
        extract = BdsReindexer._extract_block_txs

        self.assertEqual(extract({"data": {"txs": ["a"]}}), ["a"])
        self.assertEqual(extract({"data": {"txs": None}}), [])
        self.assertEqual(extract({"data": ["b"]}), ["b"])
        self.assertEqual(extract({"data": 42}), [])

    def test_decode_tx_result_data_handles_empty_and_non_dict(self):
        decode = BdsReindexer._decode_tx_result_data

        self.assertIsNone(decode(None))
        self.assertIsNone(decode(""))
        non_dict = base64.b64encode(b"[1, 2]").decode("utf-8")
        self.assertIsNone(decode(non_dict))

    def test_is_indexable_tx_result_requires_consensus_fields(self):
        is_indexable = BdsReindexer._is_indexable_tx_result

        self.assertFalse(is_indexable(None))
        self.assertFalse(is_indexable({"status": 0}))
        self.assertTrue(
            is_indexable({"status": 0, "state": [], "chi_used": 0})
        )


class ParseRfc3339NanoTests(unittest.TestCase):
    def test_parses_z_suffix_without_fraction(self):
        parsed = parse_rfc3339_nano("2026-01-01T00:00:04Z")

        self.assertEqual(parsed.isoformat(), "2026-01-01T00:00:04+00:00")

    def test_truncates_nanosecond_fraction(self):
        parsed = parse_rfc3339_nano("2026-01-01T00:00:04.123456789Z")

        self.assertEqual(parsed.microsecond, 123456)

    def test_parses_explicit_positive_offset(self):
        parsed = parse_rfc3339_nano("2026-01-01T02:00:04.5+02:00")

        self.assertEqual(parsed.isoformat(), "2026-01-01T00:00:04.500000+00:00")

    def test_parses_explicit_negative_offset(self):
        parsed = parse_rfc3339_nano("2025-12-31T22:00:04.25-02:00")

        self.assertEqual(parsed.isoformat(), "2026-01-01T00:00:04.250000+00:00")

    def test_datetime_to_nanos_round_trips(self):
        parsed = parse_rfc3339_nano("2026-01-01T00:00:04.123456Z")

        nanos = datetime_to_nanos(parsed)

        self.assertEqual(nanos, 1767225604123456000)


class CometBftRpcClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoints_delegate_to_get(self):
        client = CometBftRpcClient("tcp://0.0.0.0:26657")
        self.assertEqual(client.rpc_url, "http://0.0.0.0:26657")

        calls = []

        async def _get(path, params=None):
            calls.append((path, params))
            return {"sync_info": {"latest_block_height": "12"}}

        with mock.patch.object(client, "_get", side_effect=_get):
            height = await client.latest_height()
            await client.block(3)
            await client.block_results(4)

        self.assertEqual(height, 12)
        self.assertEqual(
            calls,
            [
                ("status", None),
                ("block", {"height": "3"}),
                ("block_results", {"height": "4"}),
            ],
        )

    async def test_close_shuts_down_session_once(self):
        client = CometBftRpcClient("http://127.0.0.1:26657")
        session = mock.AsyncMock()
        client._session = session

        await client.close()
        await client.close()

        session.close.assert_awaited_once()
        self.assertIsNone(client._session)


class RunBdsReindexTests(unittest.IsolatedAsyncioTestCase):
    def _constants(self):
        return SimpleNamespace(STORAGE_HOME="/tmp/xian-test-storage")

    async def test_returns_early_when_caught_up(self):
        bds = mock.AsyncMock()
        block_source = mock.AsyncMock()
        block_source.rpc_url = "http://127.0.0.1:26657"
        plan = ReindexPlan(
            latest_height=10,
            indexed_height=10,
            start_height=11,
            end_height=10,
        )
        reindexer = mock.AsyncMock()
        reindexer.build_plan.return_value = plan

        with (
            mock.patch("xian.services.bds.reindex.load_genesis_data", return_value={}),
            mock.patch("xian.services.bds.reindex.resolve_bds_config"),
            mock.patch("xian.services.bds.reindex.BDS", return_value=bds),
            mock.patch("xian.services.bds.reindex.Driver"),
            mock.patch("xian.services.bds.reindex.StatePatchManager"),
            mock.patch(
                "xian.services.bds.reindex.resolve_state_patch_dir",
                return_value="/tmp/patches",
            ),
            mock.patch(
                "xian.services.bds.reindex.resolve_rpc_url",
                return_value="http://127.0.0.1:26657",
            ),
            mock.patch(
                "xian.services.bds.reindex.CometBftRpcClient",
                return_value=block_source,
            ) as client_cls,
            mock.patch(
                "xian.services.bds.reindex.BdsReindexer",
                return_value=reindexer,
            ) as reindexer_cls,
        ):
            result = await run_bds_reindex(constants=self._constants())

        self.assertIs(result, plan)
        # Source and trusted URL are identical, so only one client exists and
        # no trusted source is wired up.
        client_cls.assert_called_once()
        self.assertIsNone(reindexer_cls.call_args.kwargs["trusted_block_source"])
        reindexer.reindex_range.assert_not_awaited()
        block_source.close.assert_awaited_once()
        bds.close.assert_awaited_once()

    async def test_reindexes_with_trusted_source_for_remote_rpc(self):
        bds = mock.AsyncMock()
        source_client = mock.AsyncMock()
        source_client.rpc_url = "http://remote:26657"
        trusted_client = mock.AsyncMock()
        trusted_client.rpc_url = "http://127.0.0.1:26657"
        plan = ReindexPlan(
            latest_height=10,
            indexed_height=2,
            start_height=3,
            end_height=10,
        )
        reindexer = mock.AsyncMock()
        reindexer.build_plan.return_value = plan

        with (
            mock.patch("xian.services.bds.reindex.load_genesis_data", return_value={}),
            mock.patch("xian.services.bds.reindex.resolve_bds_config"),
            mock.patch("xian.services.bds.reindex.BDS", return_value=bds),
            mock.patch("xian.services.bds.reindex.Driver"),
            mock.patch("xian.services.bds.reindex.StatePatchManager"),
            mock.patch(
                "xian.services.bds.reindex.resolve_state_patch_dir",
                return_value="/tmp/patches",
            ),
            mock.patch(
                "xian.services.bds.reindex.resolve_rpc_url",
                side_effect=["http://remote:26657", "http://127.0.0.1:26657"],
            ),
            mock.patch(
                "xian.services.bds.reindex.CometBftRpcClient",
                side_effect=[source_client, trusted_client],
            ),
            mock.patch(
                "xian.services.bds.reindex.BdsReindexer",
                return_value=reindexer,
            ) as reindexer_cls,
        ):
            result = await run_bds_reindex(
                constants=self._constants(),
                rpc_url="http://remote:26657",
            )

        self.assertIs(result, plan)
        self.assertIs(
            reindexer_cls.call_args.kwargs["trusted_block_source"],
            trusted_client,
        )
        reindexer.reindex_range.assert_awaited_once_with(plan)
        source_client.close.assert_awaited_once()
        trusted_client.close.assert_awaited_once()
        bds.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
