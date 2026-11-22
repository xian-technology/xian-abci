import json
import logging
import random
import unittest
from io import BytesIO
from unittest.mock import patch

import nacl.encoding
import nacl.signing
import pytest
import xian_vm_core
from loguru import logger
from xian_runtime_types.encoding import decode, encode

from abci.server import ProtocolHandler
from abci.utils import read_messages
from tendermint.abci.types_pb2 import (
    CheckTxType,
    Request,
    RequestCheckTx,
    RequestFinalizeBlock,
    RequestInitChain,
    RequestPrepareProposal,
    RequestProcessProposal,
    Response,
    ResponsePrepareProposal,
    ResponseProcessProposal,
)
from xian.fee_policy import DEFAULT_MAX_BLOCK_CHI, DEFAULT_MAX_TX_CHI, TxFeePolicy
from xian.methods import prepare_proposal
from xian.perf import PerfTracker
from xian.utils import tx as tx_utils
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
    memo: str | None = None,
    contract: str = "currency",
    function: str = "transfer",
    kwargs: dict | None = None,
    seed: bytes = SEED,
) -> bytes:
    signing_key = nacl.signing.SigningKey(seed)
    sender = signing_key.verify_key.encode(
        encoder=nacl.encoding.HexEncoder
    ).decode("ascii")

    payload = {
        "chain_id": chain_id,
        "contract": contract,
        "function": function,
        "kwargs": {"amount": "1", "to": sender} if kwargs is None else kwargs,
        "nonce": nonce,
        "sender": sender,
        "chi_supplied": chi_supplied,
    }
    if memo is not None:
        payload["kwargs"]["memo"] = memo
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


@pytest.mark.usefixtures("node_fixture")
class TestProposalValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await Xian.create(constants=self.constants)
        self.addCleanup(self.app.client.close)
        self.addAsyncCleanup(self.app.close)
        self.app.current_block_meta = {
            "height": 1,
            "nanos": 0,
            "hash": "00" * 32,
            "chain_id": "xian-testnet-1",
        }
        self.app.chain_id = "xian-testnet-1"
        self.handler = ProtocolHandler(self.app)


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
                    max_tx_bytes=4 * 1024 * 1024,
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
                    max_tx_bytes=4 * 1024 * 1024,
                    txs=[valid_tx, oversized_tx]
                )
            ),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [valid_tx])

    async def test_prepare_proposal_reports_skipped_transactions_once(self):
        # A mempool far larger than one block is the normal case under load;
        # one log record per skipped transaction made proposals miss the
        # propose timeout, so the proposer reports skips once per proposal.
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=150,
        )
        included = make_signed_tx_bytes(nonce=0, chi_supplied=100)
        over_block_chi = make_signed_tx_bytes(nonce=1, chi_supplied=100)
        nonce_gap = make_signed_tx_bytes(nonce=2, chi_supplied=10)
        invalid = make_signed_tx_bytes(nonce=1, chi_supplied=10, mutate_signature=True)
        over_block_bytes = make_signed_tx_bytes(nonce=1, chi_supplied=10, memo="x" * 1000)
        skipped = [over_block_chi, nonce_gap, invalid]
        max_tx_bytes = len(included) + max(map(len, skipped))
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
        self.addCleanup(logger.remove, sink)

        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    max_tx_bytes=max_tx_bytes,
                    txs=[included, over_block_bytes, *skipped],
                )
            ),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [included])
        proposal_records = [
            record for record in records if record["extra"].get("stage") == "prepare_proposal"
        ]
        self.assertEqual(len(proposal_records), 1)
        counts = proposal_records[0]["extra"]
        self.assertEqual(
            (
                counts["included_txs"],
                counts["skipped_over_block_chi"],
                counts["skipped_nonce"],
                counts["skipped_invalid"],
                counts["skipped_over_block_bytes"],
            ),
            (1, 1, 1, 1, 1),
        )

    async def test_prepare_proposal_does_not_admit_candidates_after_exact_chi_fill(self):
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=150,
        )
        first = make_signed_tx_bytes(nonce=0, chi_supplied=100)
        too_expensive = make_signed_tx_bytes(nonce=1, chi_supplied=75)
        fits = make_signed_tx_bytes(nonce=1, chi_supplied=50)
        tail = make_signed_tx_bytes(nonce=2, chi_supplied=1)
        request = Request(
            prepare_proposal=RequestPrepareProposal(
                txs=[first, too_expensive, fits, tail, b"invalid"],
                max_tx_bytes=1_000_000,
            )
        )
        with patch.object(prepare_proposal, "_admit", wraps=prepare_proposal._admit) as admit:
            response = await self.process_request("prepare_proposal", request)

        self.assertEqual(list(response.prepare_proposal.txs), [first, fits])
        self.assertEqual([call.args[1] for call in admit.call_args_list], [first, too_expensive, fits])

    async def test_prepare_proposal_does_not_admit_candidates_after_exact_byte_fill(self):
        first = make_signed_tx_bytes(nonce=0)
        request = Request(
            prepare_proposal=RequestPrepareProposal(
                txs=[first, b""],
                max_tx_bytes=len(first),
            )
        )
        with patch.object(prepare_proposal, "_admit", wraps=prepare_proposal._admit) as admit:
            response = await self.process_request("prepare_proposal", request)

        self.assertEqual(list(response.prepare_proposal.txs), [first])
        self.assertEqual([call.args[1] for call in admit.call_args_list], [first])

    async def test_prepare_proposal_budget_filter_matches_original_order(self):
        if tx_utils._decode_transaction_for_proposal is None:
            self.skipTest("the installed native VM has no proposal budget filter")
        self.app.tx_fee_policy = TxFeePolicy.paid_metered(max_tx_chi=100, max_block_chi=150)
        first = make_signed_tx_bytes(nonce=0, chi_supplied=100)
        fits_late = make_signed_tx_bytes(nonce=1, chi_supplied=30)
        fits_other_sender = make_signed_tx_bytes(nonce=0, chi_supplied=20, seed=bytes(32))
        candidates = [
            make_signed_tx_bytes(nonce=1, chi_supplied=51),
            make_signed_tx_bytes(nonce=2, chi_supplied=6),
            make_signed_tx_bytes(nonce=1, chi_supplied=20, mutate_signature=True),
            make_signed_tx_bytes(nonce=1, chi_supplied=70, mutate_signature=True),
            make_signed_tx_bytes(nonce=2, chi_supplied=2, memo="x" * 2000),
            make_signed_tx_bytes_with_raw_spacing(nonce=1),
            b"malformed wire bytes",
        ]
        rng = random.Random(71)
        for budget in [2400, 3000, 1_000_000]:
            for _ in range(4):
                rng.shuffle(candidates)
                req = RequestPrepareProposal(
                    txs=[first, *candidates, fits_late, fits_other_sender], max_tx_bytes=budget
                )
                with patch.object(tx_utils, "_decode_transaction_for_proposal", None):
                    original = await self.app.prepare_proposal(req)
                optimized = await self.app.prepare_proposal(req)
                self.assertEqual(optimized.SerializeToString(), original.SerializeToString())
                self.assertIn(fits_late, optimized.txs)
                if budget == 1_000_000:
                    self.assertIn(fits_other_sender, optimized.txs)

    async def test_prepare_proposal_filters_duplicate_nonce(self):
        tx_one = make_signed_tx_bytes(nonce=0)
        tx_two = make_signed_tx_bytes(nonce=0)

        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(max_tx_bytes=4 * 1024 * 1024, txs=[tx_one, tx_two])
            ),
        )

        self.assertEqual(list(response.prepare_proposal.txs), [tx_one])

    async def test_prepare_proposal_filters_default_json_wire_format(
        self,
    ):
        tx = make_signed_tx_bytes_with_raw_spacing(nonce=0)

        response = await self.process_request(
            "prepare_proposal",
            Request(prepare_proposal=RequestPrepareProposal(max_tx_bytes=4 * 1024 * 1024, txs=[tx])),
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

    async def test_prepare_proposal_respects_total_byte_budget(self):
        txs = [make_signed_tx_bytes(nonce=0), make_signed_tx_bytes(nonce=1)]
        total = sum(map(len, txs))
        for budget, expected in [
            (0, []),
            (len(txs[0]) - 1, []),
            (len(txs[0]), txs[:1]),
            (total - 1, txs[:1]),
            (total, txs),
        ]:
            with self.subTest(budget=budget):
                response = await self.process_request(
                    "prepare_proposal",
                    Request(
                        prepare_proposal=RequestPrepareProposal(
                            txs=txs,
                            max_tx_bytes=budget,
                        )
                    ),
                )
                self.assertEqual(list(response.prepare_proposal.txs), expected)
                self.assertLessEqual(sum(map(len, response.prepare_proposal.txs)), budget)

    async def test_prepare_proposal_skipped_size_does_not_consume_nonce_or_chi(self):
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=100,
        )
        oversized = make_signed_tx_bytes(nonce=0, memo="x" * 100)
        successor = make_signed_tx_bytes(nonce=1)
        replacement = make_signed_tx_bytes(nonce=0)
        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    txs=[oversized, successor, replacement],
                    max_tx_bytes=len(replacement),
                )
            ),
        )
        self.assertEqual(list(response.prepare_proposal.txs), [replacement])
        accepted = await self.process_request(
            "process_proposal",
            Request(
                process_proposal=RequestProcessProposal(
                    txs=response.prepare_proposal.txs,
                )
            ),
        )
        self.assertEqual(accepted.process_proposal.status, ResponseProcessProposal.ACCEPT)

    async def test_prepare_proposal_invalid_transaction_does_not_consume_bytes(self):
        invalid = make_signed_tx_bytes(nonce=0, mutate_signature=True)
        valid = make_signed_tx_bytes(nonce=0)
        response = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    txs=[invalid, valid],
                    max_tx_bytes=len(valid),
                )
            ),
        )
        self.assertEqual(list(response.prepare_proposal.txs), [valid])

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

    async def test_genesis_paid_policy_caps_bound_every_proposal_stage(self):
        # The fixture genesis carries the default paid policy (200k/500k).
        self.assertEqual(self.app.tx_fee_policy.mode, "paid_metered")
        tx_cap, block_cap = DEFAULT_MAX_TX_CHI, DEFAULT_MAX_BLOCK_CHI
        self.assertEqual(
            (self.app.tx_fee_policy.max_tx_chi, self.app.tx_fee_policy.max_block_chi),
            (tx_cap, block_cap),
        )
        first = make_signed_tx_bytes(nonce=0, chi_supplied=tx_cap)
        second = make_signed_tx_bytes(nonce=1, chi_supplied=tx_cap)
        too_big_for_block = make_signed_tx_bytes(nonce=2, chi_supplied=tx_cap)
        fills_block = make_signed_tx_bytes(nonce=2, chi_supplied=block_cap - 2 * tx_cap)
        over_tx_cap = make_signed_tx_bytes(nonce=0, chi_supplied=tx_cap + 1)

        prepared = await self.process_request(
            "prepare_proposal",
            Request(
                prepare_proposal=RequestPrepareProposal(
                    max_tx_bytes=4 * 1024 * 1024,
                    txs=[over_tx_cap, first, second, too_big_for_block, fills_block],
                )
            ),
        )
        self.assertEqual(list(prepared.prepare_proposal.txs), [first, second, fills_block])

        for txs, status in (
            (list(prepared.prepare_proposal.txs), ResponseProcessProposal.ACCEPT),
            ([first, second, too_big_for_block], ResponseProcessProposal.REJECT),
            ([over_tx_cap], ResponseProcessProposal.REJECT),
        ):
            with self.subTest(txs=len(txs), status=status):
                processed = await self.process_request(
                    "process_proposal",
                    Request(process_proposal=RequestProcessProposal(txs=txs)),
                )
                self.assertEqual(processed.process_proposal.status, status)

        with patch.object(self.app.tx_processor, "process_block") as process_block:
            finalized = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=[over_tx_cap], height=1)),
            )
        process_block.assert_not_called()
        receipt = json.loads(finalized.finalize_block.tx_results[0].data.decode("utf-8"))
        self.assertIn("Invalid transaction in block", receipt["error"])
        self.assertIn(f"per-transaction limit of {tx_cap} chi", receipt["error"])

    async def test_finalize_block_rejects_tx_past_the_block_cap_before_execution(self):
        self.app.tx_fee_policy = TxFeePolicy.paid_metered(max_tx_chi=100, max_block_chi=150)
        txs = [make_signed_tx_bytes(nonce=0), make_signed_tx_bytes(nonce=1)]
        ok_result = {
            "tx_result": {
                "hash": "ABC123",
                "status": 0,
                "state": [],
                "events": [],
                "chi_used": 1,
                "result": "ok",
            }
        }

        with (
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=lambda txs, _raw, **_kw: [ok_result for _ in txs],
            ) as process_block,
            patch.object(self.app.nonce_storage, "set_nonce_by_tx"),
        ):
            finalized = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=txs)),
            )

        process_block.assert_called_once()
        self.assertEqual(len(process_block.call_args.args[0]), 1)
        results = finalized.finalize_block.tx_results
        self.assertEqual(results[0].code, self.constants.OkCode)
        self.assertEqual(results[1].code, self.constants.ErrorCode)
        receipt = json.loads(results[1].data.decode("utf-8"))
        self.assertIn("would exceed the chain's per-block limit of 150 chi", receipt["error"])

    async def test_finalize_block_rejects_invalid_signature_before_execution(
        self,
    ):
        invalid_tx = make_signed_tx_bytes(nonce=0, mutate_signature=True)

        with patch.object(self.app.tx_processor, "process_block") as process_block:
            response = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=[invalid_tx])),
            )
        process_block.assert_not_called()

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        tx_result = response.finalize_block.tx_results[0]
        self.assertEqual(tx_result.code, self.constants.ErrorCode)
        error_payload = json.loads(tx_result.data.decode("utf-8"))
        self.assertIn("Error decoding transaction", error_payload["error"])
        self.assertIn("Bad signature", error_payload["error"])

    async def test_finalize_block_rejects_default_json_wire_format_before_execution(
        self,
    ):
        tx = make_signed_tx_bytes_with_raw_spacing(nonce=0)

        with patch.object(self.app.tx_processor, "process_block") as process_block:
            response = await self.process_request(
                "finalize_block",
                Request(finalize_block=RequestFinalizeBlock(txs=[tx])),
            )
        process_block.assert_not_called()

        self.assertEqual(len(response.finalize_block.tx_results), 1)
        tx_result = response.finalize_block.tx_results[0]
        self.assertEqual(tx_result.code, self.constants.ErrorCode)
        error_payload = json.loads(tx_result.data.decode("utf-8"))
        self.assertIn("Transaction bytes are not canonical", error_payload["error"])

    async def test_finalize_block_rejects_duplicate_nonce_before_execution(
        self,
    ):
        tx_one = make_signed_tx_bytes(nonce=0)
        tx_two = make_signed_tx_bytes(nonce=0)

        ok_result = {
            "tx_result": {
                "hash": "ABC123",
                "status": 0,
                "state": [],
                "events": [],
                "chi_used": 1,
                "result": "ok",
            }
        }
        with (
            patch.object(
                self.app.tx_processor,
                "process_block",
                side_effect=lambda txs, _raw, **_kw: [ok_result for _ in txs],
            ) as process_block,
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
        self.assertEqual(response.finalize_block.tx_results[0].code, self.constants.OkCode)
        second_result = response.finalize_block.tx_results[1]
        self.assertEqual(second_result.code, self.constants.ErrorCode)
        error_payload = json.loads(second_result.data.decode("utf-8"))
        self.assertIn("Expected 1, got 0", error_payload["error"])
        process_block.assert_called_once()
        self.assertEqual(len(process_block.call_args.args[0]), 1)
        set_nonce.assert_called_once()

    async def test_finalize_block_charges_and_consumes_nonce_for_missing_contract(self):
        # A contract that does not exist is a normal failed transaction: it
        # pays base chi and its nonce cannot be included again for free.
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        tx = make_signed_tx_bytes(nonce=0, contract="con_missing", function="any")
        sender = (
            nacl.signing.SigningKey(SEED)
            .verify_key.encode(encoder=nacl.encoding.HexEncoder)
            .decode("ascii")
        )

        response = await self.process_request(
            "finalize_block",
            Request(finalize_block=RequestFinalizeBlock(txs=[tx], height=1)),
        )

        tx_result = response.finalize_block.tx_results[0]
        self.assertEqual(tx_result.code, 1)
        receipt = json.loads(tx_result.data.decode("utf-8"))
        self.assertEqual(receipt["status"], 1)
        self.assertEqual(receipt["chi_used"], 5)
        self.assertIn("con_missing", receipt["result"])
        await self.app.commit()
        self.assertEqual(self.app.nonce_storage.get_nonce(sender), 0)

    async def test_immutable_admission_is_reused_without_caching_live_policy(self):
        cache = xian_vm_core.TransactionAdmissionCache(
            self.app.chain_id, max_entries=2, max_bytes=64_000
        )
        self.app.transaction_admission_cache = cache
        permissive = TxFeePolicy.free_metered(max_tx_chi=100, max_block_chi=100)
        self.app.tx_fee_policy = permissive
        tx = make_signed_tx_bytes(nonce=0, contract="con_missing", function="any")

        self.assertEqual((await self.app.check_tx(tx)).code, self.constants.OkCode)
        prepared = await self.app.prepare_proposal(
            RequestPrepareProposal(txs=[tx], max_tx_bytes=len(tx))
        )
        self.assertEqual(list(prepared.txs), [tx])
        proposed = RequestProcessProposal(txs=[tx])
        self.app.tx_fee_policy = TxFeePolicy.free_metered(max_tx_chi=99, max_block_chi=100)
        self.assertEqual(
            (await self.app.process_proposal(proposed)).status, ResponseProcessProposal.REJECT
        )
        self.app.tx_fee_policy = permissive
        self.assertEqual(
            (await self.app.process_proposal(proposed)).status, ResponseProcessProposal.ACCEPT
        )

        finalized = await self.app.finalize_block(RequestFinalizeBlock(txs=[tx], height=1))
        self.assertIn("con_missing", json.loads(finalized.tx_results[0].data)["result"])
        stats = cache.stats()
        self.assertEqual((stats["hits"], stats["misses"]), (4, 1))

    async def test_check_tx_profiling_distinguishes_new_transactions_and_rechecks(self):
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        self.app.profiler = PerfTracker(
            output_path=self.constants.COMETBFT_HOME / "perf-test.json",
            node_name="test",
            chain_id=self.app.chain_id,
            execution_mode=self.app.execution_mode,
        )
        tx = make_signed_tx_bytes(nonce=0)
        for check_type in (CheckTxType.NEW, CheckTxType.RECHECK):
            response = await self.process_request(
                "check_tx", Request(check_tx=RequestCheckTx(tx=tx, type=check_type))
            )
            self.assertEqual(response.check_tx.code, self.constants.OkCode)

        metrics = self.app.profiler.snapshot()["global_metrics"]
        self.assertEqual(metrics["check_tx_new"]["count"], 1)
        self.assertEqual(metrics["check_tx_recheck"]["count"], 1)
        for metric in ("check_tx", "check_tx_lock_wait", "check_tx_static_admission", "check_tx_live_admission"):
            self.assertEqual(metrics[metric]["count"], 2)

    async def test_finalize_block_halts_when_processing_raises(self):
        tx = make_signed_tx_bytes(nonce=0)
        with patch.object(self.app.tx_processor, "process_block", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await self.app.finalize_block(RequestFinalizeBlock(txs=[tx], height=1))
        self.assertIsNone(self.app._pending_commit_driver_state)
        self.assertFalse(self.app._state_transition_lock.locked())

    async def test_paid_rejected_deployments_have_deterministic_error_receipts(self):
        from google.protobuf.timestamp_pb2 import Timestamp
        timestamp = Timestamp()
        timestamp.FromJsonString(self.app.genesis["genesis_time"])
        await self.app.init_chain(RequestInitChain(time=timestamp))
        sender = nacl.signing.SigningKey(SEED).verify_key.encode().hex()
        driver = self.app.client.raw_driver
        driver.set(f"currency.balances:{sender}", 1_000_000)
        driver.commit()
        self.app.state_root_cache.rebuild_from_driver(driver)
        source = "a=1\nb=2\n@export\ndef a():\n    return 1\n@export\ndef b():\n    return 2\n"
        errors = []

        for nonce in range(16):
            tx = make_signed_tx_bytes(
                nonce=nonce,
                contract="submission",
                function="submit_contract",
                chi_supplied=200_000,
                kwargs={"name": "con_attack", "code": source},
            )
            response = await self.app.finalize_block(
                RequestFinalizeBlock(txs=[tx], height=nonce + 1, time=Timestamp(
                    seconds=timestamp.seconds + nonce + 1, nanos=timestamp.nanos))
            )
            receipt = json.loads(response.tx_results[0].data)
            self.assertEqual(receipt["status"], 1)
            self.assertGreater(receipt["chi_used"], 0)
            self.assertIn("conflicting module binding", receipt["result"])
            errors.append(receipt["result"])
            await self.app.commit()

        self.assertEqual(len(set(errors)), 1, errors)
        self.assertIsNone(driver.get("con_attack.__source__"))
        self.assertEqual(self.app.nonce_storage.get_nonce(sender), 15)

    async def test_finalize_host_fault_discards_partial_state_before_replay(self):
        driver = self.app.client.raw_driver
        driver.set("currency.balances:alice", 123)
        driver.commit()
        committed_root = self.app.state_root_cache.rebuild_from_driver(driver)

        def partially_execute(*args, **kwargs):
            driver.set("currency.balances:alice", 999)
            driver.set("__n.alice:", 7)
            raise OSError("injected host failure after first transaction")

        with patch.object(self.app.tx_processor, "process_block", side_effect=partially_execute):
            with self.assertRaises(OSError):
                await self.app.finalize_block(
                    RequestFinalizeBlock(txs=[make_signed_tx_bytes(nonce=0)], height=1)
                )

        self.assertEqual(driver.pending_items(), [])
        self.assertEqual(driver.get("currency.balances:alice"), 123)
        self.assertIsNone(driver.get("__n.alice:"))
        self.assertEqual(self.app.state_root_cache.root_hash, committed_root)
        self.assertIsNone(self.app.current_block_meta)
        self.assertIsNone(self.app.merkle_root_hash)

    async def test_local_mempool_size_limit_does_not_change_block_validation(self):
        # mempool.max_tx_bytes is node-local. Consensus block size already
        # bounds transactions, so validators with different limits must agree.
        self.app.tx_fee_policy = TxFeePolicy.free_metered()
        tx = make_signed_tx_bytes(nonce=0, contract="con_missing", function="any")
        self.app.max_tx_bytes = len(tx) - 1

        process_response = await self.process_request(
            "process_proposal",
            Request(process_proposal=RequestProcessProposal(txs=[tx])),
        )
        self.assertEqual(
            process_response.process_proposal.status,
            ResponseProcessProposal.ProposalStatus.ACCEPT,
        )
        finalize_response = await self.process_request(
            "finalize_block",
            Request(finalize_block=RequestFinalizeBlock(txs=[tx], height=1)),
        )
        receipt = json.loads(finalize_response.finalize_block.tx_results[0].data)
        self.assertEqual(receipt["chi_used"], 5)
