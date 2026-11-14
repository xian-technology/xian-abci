import logging
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from xian_runtime_types.decimal import ContractingDecimal

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta1.types_pb2 import RequestCommit, RequestQuery
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    Response,
)
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)


def per_tx(process_tx):
    """A ``process_block`` stand-in that processes each transaction alone."""
    return lambda txs, raw_txs, **kwargs: [process_tx(tx) for tx in txs]


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


@pytest.mark.usefixtures("node_fixture")
class TestFinalizeBlock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await Xian.create(constants=self.constants)
        self.addCleanup(self.app.client.close)
        self.addAsyncCleanup(self.app.close)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.handler = ProtocolHandler(self.app)

    async def process_request(self, req):
        raw = await self.handler.process("finalize_block", req)
        return await deserialize(raw)

    async def test_finalize_block_emits_standard_contract_events(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [
                {"key": "currency.balances:alice", "value": "99"},
            ],
            "events": [
                {
                    "event": "Transfer",
                    "contract": "currency",
                    "signer": "alice",
                    "caller": "alice",
                    "data_indexed": {"to": "bob"},
                    "data": {"amount": "10"},
                }
            ],
        }

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={"payload": {"chi_supplied": 100}},
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        tx_events = response.finalize_block.tx_results[0].events
        self.assertEqual([event.type for event in tx_events], ["StateChange", "Transfer"])

        transfer_event = tx_events[1]
        attrs = {attr.key: attr.value for attr in transfer_event.attributes}
        self.assertEqual(attrs["contract"], "currency")
        self.assertEqual(attrs["signer"], "alice")
        self.assertEqual(attrs["caller"], "alice")
        self.assertEqual(attrs["to"], "bob")
        self.assertEqual(attrs["amount"], "10")

    async def test_finalize_block_formats_decimal_event_values_plainly(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [],
            "events": [
                {
                    "event": "Transfer",
                    "contract": "currency",
                    "signer": "alice",
                    "caller": "alice",
                    "data_indexed": {"to": "bob"},
                    "data": {"amount": ContractingDecimal("5000")},
                }
            ],
        }

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={"payload": {"chi_supplied": 100}},
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        transfer_event = response.finalize_block.tx_results[0].events[0]
        attrs = {attr.key: attr.value for attr in transfer_event.attributes}
        self.assertEqual(attrs["amount"], "5000")

    async def test_finalize_stages_writes_and_releases_lock_before_commit(self):
        tx = {
            "payload": {
                "sender": "alice",
                "nonce": 0,
                "contract": "currency",
                "function": "transfer",
                "chi_supplied": 100,
                "kwargs": {},
            }
        }
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [
                {"key": "currency.balances:alice", "value": 99},
            ],
            "events": [],
        }

        def process_tx(*args, **kwargs):
            del args, kwargs
            self.app.client.raw_driver.apply_writes({"currency.balances:alice": 99})
            return {"tx_result": tx_result}

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value=tx,
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(process_tx),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            await self.process_request(Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"])))

        self.assertFalse(self.app._state_transition_lock.locked())
        self.assertEqual(self.app.client.raw_driver.pending_items(), [])
        self.assertIsNotNone(self.app._pending_commit_driver_state)
        self.assertEqual(
            [key for key, _ in self.app._pending_commit_driver_state["pending"].items()],
            ["currency.balances:alice"],
        )
        self.assertIsNone(self.app.client.raw_driver.get("currency.balances:alice"))

        raw = await self.handler.process("commit", Request(commit=RequestCommit()))
        response = await deserialize(raw)

        self.assertEqual(response.commit.retain_height, 0)
        self.assertIsNone(self.app._pending_commit_driver_state)
        self.assertEqual(
            self.app.client.raw_driver.get("currency.balances:alice"),
            99,
        )

    async def test_finalize_block_halts_when_processor_returns_no_receipt(self):
        # Deterministic failures become receipts inside the processor. A
        # missing receipt or hash is a broken invariant, never a free tx.
        tx = {
            "payload": {
                "sender": "alice",
                "nonce": 1,
                "contract": "currency",
                "function": "transfer",
                "chi_supplied": 100,
            },
            "metadata": {"signature": "sig"},
        }
        no_hash = {"status": 0, "state": [], "events": [], "chi_used": 1, "result": "ok"}
        for tx_result in (None, no_hash):
            with self.subTest(tx_result=tx_result):
                with (
                    patch(
                        "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                        return_value=dict(tx),
                    ),
                    patch(
                        "xian.methods.finalize_block.validate_consensus_transaction_after_static"
                    ),
                    patch.object(
                        self.app.tx_processor,
                        "process_block",
                        side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
                    ),
                    patch.object(self.app.nonce_storage, "set_nonce_by_tx") as set_nonce,
                ):
                    with self.assertRaisesRegex(RuntimeError, "no receipt"):
                        await self.app.finalize_block(RequestFinalizeBlock(txs=[b"dummy"]))
                set_nonce.assert_not_called()
                self.assertIsNone(self.app._pending_commit_driver_state)
                self.app.client.raw_driver.flush_cache()

    async def test_finalize_block_app_hash_commits_state_writes(self):
        base_result = {
            "hash": "SAME_TX_HASH",
            "status": 0,
            "state": [{"key": "currency.balances:alice", "value": "99"}],
            "events": [],
            "chi_used": 1,
            "result": "ok",
        }
        changed_result = {
            **base_result,
            "state": [{"key": "currency.balances:alice", "value": "98"}],
        }

        def process_result(tx_result):
            for state_write in tx_result["state"]:
                self.app.client.raw_driver.set(
                    state_write["key"],
                    state_write["value"],
                )
            return {"tx_result": tx_result}

        async def finalize_with(tx_result):
            with (
                patch(
                    "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                    return_value={
                        "payload": {
                            "sender": "alice",
                            "nonce": 1,
                            "contract": "currency",
                            "function": "transfer",
                            "chi_supplied": 100,
                        },
                        "metadata": {"signature": "sig"},
                    },
                ),
                patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
                patch.object(
                    self.app.tx_processor,
                    "process_block",
                    side_effect=per_tx(lambda tx: process_result(tx_result)),
                ),
                patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
            ):
                response = await self.process_request(
                    Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
                )
            await self.handler.process("commit", Request(commit=RequestCommit()))
            return response.finalize_block.app_hash

        first_hash = await finalize_with(base_result)
        second_hash = await finalize_with(changed_result)

        self.assertNotEqual(first_hash, second_hash)

    async def test_query_reads_committed_state_between_finalize_and_commit(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [{"key": "currency.balances:alice", "value": "99"}],
            "events": [],
            "chi_used": 1,
            "result": "ok",
        }

        def process_result():
            for state_write in tx_result["state"]:
                self.app.client.raw_driver.set(
                    state_write["key"],
                    state_write["value"],
                )
            return {"tx_result": tx_result}

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={
                    "payload": {
                        "sender": "alice",
                        "nonce": 1,
                        "contract": "currency",
                        "function": "transfer",
                        "chi_supplied": 100,
                    },
                    "metadata": {"signature": "sig"},
                },
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: process_result()),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"], height=7))
            )

        pre_commit_response = await deserialize(
            await self.handler.process(
                "query",
                Request(query=RequestQuery(path="/get/currency.balances:alice")),
            )
        )
        self.assertEqual(pre_commit_response.query.value, b"")
        self.assertEqual(pre_commit_response.query.height, 0)

        await self.handler.process("commit", Request(commit=RequestCommit()))
        query_response = await deserialize(
            await self.handler.process(
                "query",
                Request(query=RequestQuery(path="/get/currency.balances:alice")),
            )
        )

        self.assertEqual(query_response.query.value, b"99")
        self.assertEqual(query_response.query.height, 7)

    async def test_finalize_block_warms_shielded_proof_cache_on_serial_path(self):
        tx = {
            "payload": {
                "sender": "alice",
                "contract": "con_shielded_note_token",
                "function": "deposit_shielded",
                "chi_supplied": 100,
                "kwargs": {
                    "amount": 7,
                    "old_root": "0x" + "11" * 32,
                    "output_commitments": ["0x" + "22" * 32],
                    "proof_hex": "0xabcd",
                },
            },
            "metadata": {"signature": "sig"},
        }
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [],
            "events": [],
            "chi_used": 1,
            "result": "ok",
        }

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value=tx,
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch("xian.methods.finalize_block.warm_shielded_proof_cache") as warm_cache,
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            warm_cache.return_value = type(
                "Stats",
                (),
                {
                    "candidate_count": 1,
                    "verified_count": 1,
                    "failed_count": 0,
                },
            )()
            await self.process_request(Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"])))

        warm_cache.assert_called_once()

    async def test_finalize_block_enqueues_bds_payload_when_enabled(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [
                {"key": "currency.balances:alice", "value": "99"},
            ],
            "events": [],
            "chi_used": 1,
            "result": "ok",
        }
        self.app.bds_enabled = True
        self.app.bds = type("FakeBDS", (), {"enqueue_block": AsyncMock(), "close": AsyncMock()})()

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={
                    "payload": {
                        "sender": "alice",
                        "nonce": 1,
                        "contract": "currency",
                        "function": "transfer",
                        "chi_supplied": 100,
                    },
                    "metadata": {"signature": "sig"},
                },
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            await self.process_request(Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"])))

        self.app.bds.enqueue_block.assert_awaited_once()
        payload = self.app.bds.enqueue_block.await_args.args[0]
        self.assertEqual(payload.block_meta["height"], 0)
        self.assertEqual(payload.transactions[0].tx_index, 0)
        self.assertEqual(payload.transactions[0].payload["sender"], "alice")
        # The indexed envelope is the signed transaction without block metadata.
        self.assertEqual(set(payload.transactions[0].envelope), {"payload", "metadata"})

    async def test_finalize_block_ignores_bds_enqueue_failures(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [],
            "events": [],
            "chi_used": 1,
            "result": "ok",
        }
        self.app.bds_enabled = True
        self.app.bds = type(
            "FakeBDS",
            (),
            {
                "enqueue_block": AsyncMock(side_effect=RuntimeError("bds down")),
                "close": AsyncMock(),
            },
        )()

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={
                    "payload": {
                        "sender": "alice",
                        "nonce": 1,
                        "contract": "currency",
                        "function": "transfer",
                        "chi_supplied": 100,
                    },
                    "metadata": {"signature": "sig"},
                },
            ),
            patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=per_tx(lambda tx: {"tx_result": tx_result}),
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        self.assertEqual(response.finalize_block.tx_results[0].code, self.constants.OkCode)
        # gas fields enter CometBFT's LastResultsHash; chi is reported in data.
        self.assertEqual(response.finalize_block.tx_results[0].gas_used, 0)
        self.assertEqual(response.finalize_block.tx_results[0].gas_wanted, 0)
        self.app.bds.enqueue_block.assert_awaited_once()
