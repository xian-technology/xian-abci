import logging
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures
from xian_runtime_types.decimal import ContractingDecimal

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    Response,
)
from xian.constants import Constants as c
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class TestFinalizeBlock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.handler = ProtocolHandler(self.app)

    async def asyncTearDown(self):
        teardown_fixtures()

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
                return_value={"payload": {}},
            ),
            patch(
                "xian.methods.finalize_block.validate_consensus_transaction_after_static"
            ),
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={"tx_result": tx_result},
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        tx_events = response.finalize_block.tx_results[0].events
        self.assertEqual(
            [event.type for event in tx_events], ["StateChange", "Transfer"]
        )

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
                return_value={"payload": {}},
            ),
            patch(
                "xian.methods.finalize_block.validate_consensus_transaction_after_static"
            ),
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={"tx_result": tx_result},
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        transfer_event = response.finalize_block.tx_results[0].events[0]
        attrs = {attr.key: attr.value for attr in transfer_event.attributes}
        self.assertEqual(attrs["amount"], "5000")

    async def test_finalize_block_handles_missing_tx_result(self):
        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={"payload": {}},
            ),
            patch(
                "xian.methods.finalize_block.validate_consensus_transaction_after_static"
            ),
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={"tx_result": None},
            ),
            patch.object(
                self.app.nonce_storage, "set_nonce_by_tx"
            ) as set_nonce,
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        self.assertEqual(
            response.finalize_block.tx_results[0].code, c.ErrorCode
        )
        set_nonce.assert_not_called()

    async def test_finalize_block_enqueues_bds_payload_when_enabled(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [
                {"key": "currency.balances:alice", "value": "99"},
            ],
            "events": [],
            "stamps_used": 1,
            "result": "ok",
        }
        self.app.block_service_mode = True
        self.app.bds = type("FakeBDS", (), {"enqueue_block": AsyncMock()})()

        with (
            patch(
                "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
                return_value={
                    "payload": {
                        "sender": "alice",
                        "nonce": 1,
                        "contract": "currency",
                        "function": "transfer",
                    },
                    "metadata": {"signature": "sig"},
                },
            ),
            patch(
                "xian.methods.finalize_block.validate_consensus_transaction_after_static"
            ),
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={"tx_result": tx_result},
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        self.app.bds.enqueue_block.assert_awaited_once()
        payload = self.app.bds.enqueue_block.await_args.args[0]
        self.assertEqual(payload.block_meta["height"], 0)
        self.assertEqual(payload.transactions[0].tx_index, 0)
        self.assertEqual(payload.transactions[0].payload["sender"], "alice")

    async def test_finalize_block_ignores_bds_enqueue_failures(self):
        tx_result = {
            "hash": "ABC123",
            "status": 0,
            "state": [],
            "events": [],
            "stamps_used": 1,
            "result": "ok",
        }
        self.app.block_service_mode = True
        self.app.bds = type(
            "FakeBDS",
            (),
            {"enqueue_block": AsyncMock(side_effect=RuntimeError("bds down"))},
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
                    },
                    "metadata": {"signature": "sig"},
                },
            ),
            patch(
                "xian.methods.finalize_block.validate_consensus_transaction_after_static"
            ),
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={"tx_result": tx_result},
            ),
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            response = await self.process_request(
                Request(finalize_block=RequestFinalizeBlock(txs=[b"dummy"]))
            )

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        self.assertEqual(response.finalize_block.tx_results[0].code, c.OkCode)
        self.app.bds.enqueue_block.assert_awaited_once()
