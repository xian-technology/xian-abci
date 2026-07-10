import json
import logging
import unittest
from io import BytesIO
from unittest.mock import patch

import nacl.encoding
import nacl.signing
from fixtures.mock_constants import MockConstants
from utils import setup_fixtures, teardown_fixtures
from xian_runtime_types.encoding import decode, encode

from abci.server import ProtocolHandler
from abci.utils import read_messages
from cometbft.abci.v1beta2.types_pb2 import (
    ResponsePrepareProposal,
    ResponseProcessProposal,
)
from cometbft.abci.v1beta3.types_pb2 import (
    Request,
    RequestFinalizeBlock,
    RequestPrepareProposal,
    RequestProcessProposal,
    Response,
)
from xian.constants import Constants as c
from xian.fee_policy import TxFeePolicy
from xian.utils.encoding import encode_transaction_bytes
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)

SEED = bytes(range(32))


def _sort_keys_deep(value):
    if isinstance(value, dict):
        return {key: _sort_keys_deep(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys_deep(item) for item in value]
    return value


def _canonical_json(payload: dict) -> str:
    return encode(decode(encode(_sort_keys_deep(payload))))


def make_signed_tx_bytes(
    *,
    nonce: int,
    chain_id: str = "xian-testnet-1",
    chi_supplied: int = 100,
    mutate_signature: bool = False,
) -> bytes:
    signing_key = nacl.signing.SigningKey(SEED)
    sender = signing_key.verify_key.encode(
        encoder=nacl.encoding.HexEncoder
    ).decode("ascii")

    payload = {
        "chain_id": chain_id,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": "1", "to": sender},
        "nonce": nonce,
        "sender": sender,
        "chi_supplied": chi_supplied,
    }
    payload_str = _canonical_json(payload)
    signature = signing_key.sign(payload_str.encode("utf-8")).signature.hex()
    if mutate_signature:
        signature = ("00" * 64)[: len(signature)]

    tx = {
        "metadata": {"signature": signature},
        "payload": payload,
    }
    return encode_transaction_bytes(_canonical_json(tx))


def make_signed_tx_bytes_with_raw_spacing(
    *,
    nonce: int,
    chain_id: str = "xian-testnet-1",
) -> bytes:
    signing_key = nacl.signing.SigningKey(SEED)
    sender = signing_key.verify_key.encode(
        encoder=nacl.encoding.HexEncoder
    ).decode("ascii")

    payload = {
        "chain_id": chain_id,
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": "1", "to": sender},
        "nonce": nonce,
        "sender": sender,
        "chi_supplied": 100,
    }
    payload_str = _canonical_json(payload)
    signature = signing_key.sign(payload_str.encode("utf-8")).signature.hex()
    tx = {
        "metadata": {"signature": signature},
        "payload": payload,
    }
    return encode_transaction_bytes(json.dumps(tx))


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class TestProposalValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {
            "height": 1,
            "nanos": 0,
            "hash": "00" * 32,
            "chain_id": "xian-testnet-1",
        }
        self.app.chain_id = "xian-testnet-1"
        self.handler = ProtocolHandler(self.app)

    async def asyncTearDown(self):
        teardown_fixtures()

    async def process_request(self, request_type, req):
        raw = await self.handler.process(request_type, req)
        return await deserialize(raw)

    async def test_prepare_proposal_filters_invalid_signature(self):
        valid_tx = make_signed_tx_bytes(nonce=0)
        invalid_tx = make_signed_tx_bytes(nonce=1, mutate_signature=True)

        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    txs=[valid_tx, invalid_tx]
                )
            ),
        )

        self.assertIsInstance(
            response.prepare_proposal, ResponsePrepareProposal
        )
        self.assertEqual(list(response.prepare_proposal.txs), [valid_tx])

    async def test_prepare_proposal_free_metered_drops_tx_over_chi_cap(self):
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=1_000,
        )
        valid_tx = make_signed_tx_bytes(nonce=0, chi_supplied=100)
        oversized_tx = make_signed_tx_bytes(nonce=1, chi_supplied=101)

        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    txs=[valid_tx, oversized_tx]
                )
            ),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [valid_tx])

    async def test_prepare_proposal_filters_duplicate_nonce(self):
        tx_one = make_signed_tx_bytes(nonce=0)
        tx_two = make_signed_tx_bytes(nonce=0)

        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(txs=[tx_one, tx_two])
            ),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [tx_one])

    async def test_prepare_proposal_filters_default_json_wire_format(
        self,
    ):
        tx = make_signed_tx_bytes_with_raw_spacing(nonce=0)

        response = await self.process_request(
            "prepare_proposal",
            Request(prepare_proposal=RequestPrepareProposal(txs=[tx])),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [])

    async def test_process_proposal_rejects_invalid_signature(self):
        invalid_tx = make_signed_tx_bytes(nonce=0, mutate_signature=True)

        response = await self.process_request(
            "process_proposal",
            Request(process_proposal=RequestProcessProposal(txs=[invalid_tx])),
        )

        self.assertIsInstance(
            response.process_proposal, ResponseProcessProposal
        )
        self.assertEqual(
            response.process_proposal.status,
            ResponseProcessProposal.ProposalStatus.REJECT,
        )

    async def test_process_proposal_rejects_default_json_wire_format(self):
        tx = make_signed_tx_bytes_with_raw_spacing(nonce=0)

        response = await self.process_request(
            "process_proposal",
            Request(process_proposal=RequestProcessProposal(txs=[tx])),
        )

        self.assertEqual(
            response.process_proposal.status,
            ResponseProcessProposal.ProposalStatus.REJECT,
        )

    async def test_process_proposal_rejects_duplicate_nonce(self):
        tx_one = make_signed_tx_bytes(nonce=0)
        tx_two = make_signed_tx_bytes(nonce=0)

        response = await self.process_request(
            "process_proposal",
            Request(
                process_proposal=RequestProcessProposal(txs=[tx_one, tx_two])
            ),
        )

        self.assertEqual(
            response.process_proposal.status,
            ResponseProcessProposal.ProposalStatus.REJECT,
        )

    async def test_process_proposal_free_metered_rejects_block_over_chi_cap(
        self,
    ):
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=150,
        )
        tx_one = make_signed_tx_bytes(nonce=0, chi_supplied=100)
        tx_two = make_signed_tx_bytes(nonce=1, chi_supplied=100)

        response = await self.process_request(
            "process_proposal",
            Request(
                process_proposal=RequestProcessProposal(txs=[tx_one, tx_two])
            ),
        )

        self.assertEqual(
            response.process_proposal.status,
            ResponseProcessProposal.ProposalStatus.REJECT,
        )

    async def test_finalize_block_rejects_invalid_signature_before_execution(
        self,
    ):
        invalid_tx = make_signed_tx_bytes(nonce=0, mutate_signature=True)

        with patch.object(self.app.tx_processor, "process_tx") as process_tx:
            response = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=[invalid_tx])),
            )
        process_tx.assert_not_called()

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        tx_result = response.finalize_block.tx_results[0]
        self.assertEqual(tx_result.code, c.ErrorCode)
        error_payload = json.loads(tx_result.data.decode("utf-8"))
        self.assertIn("Error decoding transaction", error_payload["error"])
        self.assertIn("Bad signature", error_payload["error"])

    async def test_finalize_block_rejects_default_json_wire_format_before_execution(
        self,
    ):
        tx = make_signed_tx_bytes_with_raw_spacing(nonce=0)

        with patch.object(self.app.tx_processor, "process_tx") as process_tx:
            response = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=[tx])),
            )
        process_tx.assert_not_called()

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        tx_result = response.finalize_block.tx_results[0]
        self.assertEqual(tx_result.code, c.ErrorCode)
        error_payload = json.loads(tx_result.data.decode("utf-8"))
        self.assertIn("Transaction bytes are not canonical", error_payload["error"])

    async def test_finalize_block_rejects_duplicate_nonce_before_execution(
        self,
    ):
        tx_one = make_signed_tx_bytes(nonce=0)
        tx_two = make_signed_tx_bytes(nonce=0)

        with (
            patch.object(
                self.app.tx_processor,
                "process_tx",
                return_value={
                    "tx_result": {
                        "hash": "ABC123",
                        "status": 0,
                        "state": [],
                        "events": [],
                        "chi_used": 1,
                        "result": "ok",
                    }
                },
            ) as process_tx,
            patch.object(
                self.app.nonce_storage, "set_nonce_by_tx"
            ) as set_nonce,
        ):
            response = await self.process_request(
                "finalize_block",
                Request(
                    finalize_block=RequestFinalizeBlock(txs=[tx_one, tx_two])
                ),
            )

        self.assertEqual(len(response.finalize_block.tx_results), 2)
        self.assertEqual(response.finalize_block.tx_results[0].code, c.OkCode)
        second_result = response.finalize_block.tx_results[1]
        self.assertEqual(second_result.code, c.ErrorCode)
        error_payload = json.loads(second_result.data.decode("utf-8"))
        self.assertIn("Expected 1, got 0", error_payload["error"])
        process_tx.assert_called_once()
        set_nonce.assert_called_once()
