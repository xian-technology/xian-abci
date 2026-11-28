import json
import logging
import unittest
from io import BytesIO
from unittest.mock import patch

import nacl.encoding
import nacl.signing
import pytest
from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode, encode

from abci.server import ProtocolHandler
from abci.utils import read_messages
from tendermint.abci.types_pb2 import Request, RequestCheckTx, Response
from xian.fee_policy import DEFAULT_MAX_TX_CHI, TxFeePolicy
from xian.methods.check_tx import report_check_tx_rejections
from xian.utils.encoding import encode_transaction_bytes
from xian.utils.tx import create_transaction_admission_cache
from xian.xian_abci import Xian

logging.disable(logging.CRITICAL)

SEED = bytes(range(32))
VALID_NONCE = 6


def _sort_keys_deep(value):
    if isinstance(value, dict):
        return {key: _sort_keys_deep(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys_deep(item) for item in value]
    return value


def _canonical_json(payload: dict) -> str:
    return encode(decode(json.dumps(_sort_keys_deep(payload))))


def make_signed_tx_bytes(
    *, nonce: int, chi_supplied: int = 100, amount: int = 1, function: str = "transfer"
) -> bytes:
    return make_signed_tx_bytes_for_payload(
        {
            "chain_id": "xian-testnet-1",
            "contract": "currency",
            "function": function,
            "kwargs": {"amount": amount, "to": SENDER},
            "nonce": nonce,
            "sender": SENDER,
            "chi_supplied": chi_supplied,
        }
    )


def make_signed_tx_bytes_for_payload(payload: dict) -> bytes:
    signing_key = nacl.signing.SigningKey(SEED)
    payload_str = _canonical_json(payload)
    signature = signing_key.sign(payload_str.encode("utf-8")).signature.hex()
    tx = {"metadata": {"signature": signature}, "payload": payload}
    return encode_transaction_bytes(_canonical_json(tx))


def make_signed_tx_bytes_with_raw_spacing(*, nonce: int) -> bytes:
    signing_key = nacl.signing.SigningKey(SEED)
    payload = {
        "chain_id": "xian-testnet-1",
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": 1, "to": SENDER},
        "nonce": nonce,
        "sender": SENDER,
        "chi_supplied": 100,
    }
    payload_str = _canonical_json(payload)
    signature = signing_key.sign(payload_str.encode("utf-8")).signature.hex()
    tx = {"metadata": {"signature": signature}, "payload": payload}
    return encode_transaction_bytes(json.dumps(tx))


SENDER = (
    nacl.signing.SigningKey(SEED)
    .verify_key.encode(encoder=nacl.encoding.HexEncoder)
    .decode("ascii")
)
VALID_TX_BYTES = make_signed_tx_bytes(nonce=VALID_NONCE)


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


@pytest.mark.usefixtures("node_fixture")
class TestCheckTx(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await Xian.create(constants=self.constants)
        self.addCleanup(self.app.client.close)
        self.addAsyncCleanup(self.app.close)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.app.chain_id = "xian-testnet-1"
        self.app.transaction_admission_cache = create_transaction_admission_cache(self.app.chain_id)
        self.app.client.raw_driver.set(f"currency.balances:{SENDER}", 100000)
        # The fixture genesis rate; these tests run without InitChain.
        self.app.client.raw_driver.set("chi_cost.S:value", 20)
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
        self.app.client.raw_driver.commit()
        self.handler = ProtocolHandler(self.app)

    async def process_request(self, req):
        raw = await self.handler.process("check_tx", req)
        return await deserialize(raw)

    def make_request(self, tx_bytes=VALID_TX_BYTES):
        return Request(check_tx=RequestCheckTx(tx=tx_bytes))

    async def test_check_tx_accepts_next_nonce(self):

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, self.constants.OkCode)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_native_recheck_sees_the_next_committed_state(self):
        driver = self.app.client.raw_driver
        balance_key = f"currency.balances:{SENDER}"
        with patch(
            "xian.methods.check_tx.validate_transaction_after_static",
            side_effect=AssertionError("expected native admission"),
        ):
            accepted = await self.app.check_tx(VALID_TX_BYTES)
            self.assertEqual(accepted.code, self.constants.OkCode, accepted.log)

            driver.set(balance_key, 5)
            driver.commit()
            too_expensive = await self.app.check_tx(VALID_TX_BYTES, recheck=True)
            self.assertEqual(too_expensive.code, self.constants.ErrorCode)
            self.assertIn("plus the chi_supplied fee reservation", too_expensive.log)

            driver.set("chi_cost.S:value", 25)
            driver.commit()
            accepted = await self.app.check_tx(VALID_TX_BYTES, recheck=True)
            self.assertEqual(accepted.code, self.constants.OkCode, accepted.log)

            self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE)
            driver.commit()
            stale = await self.app.check_tx(VALID_TX_BYTES, recheck=True)
            self.assertEqual(stale.code, self.constants.ErrorCode)
            self.assertIn("Transaction nonce is invalid", stale.log)

    async def test_native_check_tx_matches_python_for_paid_and_free_admission(self):
        driver = self.app.client.raw_driver
        balance_key = f"currency.balances:{SENDER}"
        cases = (
            (None, 20, 1, 100, "transfer", True),
            (6, 20, 1, 100, "transfer", True),
            (6, 20, 2, 100, "transfer", True),
            (6, 20, None, 100, "transfer", True),
            (6, 20, "1", 100, "transfer", True),
            (6, 20, {"__fixed__": "1.5"}, 100, "approve", True),
            (6, 20, {"__fixed__": "1.5"}, 100, "transfer", True),
            (ContractingDecimal("6.5"), 20, {"__fixed__": "1.5"}, 100, "transfer", True),
            (10**70, 20, {"__big_int__": str(10**70 - 5)}, 100, "transfer", True),
            (6, None, 1, 100, "transfer", True),
            (6, 0, 1, 100, "transfer", True),
            (6, 20, 1, DEFAULT_MAX_TX_CHI + 1, "transfer", True),
            (None, None, 1, 100, "transfer", False),
            (None, None, 1, DEFAULT_MAX_TX_CHI + 1, "transfer", False),
        )
        for balance, rate, amount, chi, function, paid in cases:
            with self.subTest(balance=balance, rate=rate, amount=amount, chi=chi, paid=paid):
                driver.set(balance_key, balance)
                driver.set("chi_cost.S:value", rate)
                driver.commit()
                self.app.tx_fee_policy = (
                    TxFeePolicy.paid_metered() if paid else TxFeePolicy.free_metered()
                )
                raw = make_signed_tx_bytes(
                    nonce=VALID_NONCE, chi_supplied=chi, amount=amount, function=function
                )
                with patch.object(self.app, "transaction_admission_cache", None):
                    expected = await self.app.check_tx(raw)
                for recheck in (False, True):
                    actual = await self.app.check_tx(raw, recheck=recheck)
                    self.assertEqual(actual, expected)

    async def test_check_tx_rejects_runtime_invalid_submission_names(self):
        for name in ("con_Uppercase", "con_" + "a" * 61, None, 123):
            with self.subTest(name=name):
                tx = make_signed_tx_bytes_for_payload(
                    {
                        "chain_id": "xian-testnet-1",
                        "contract": "submission",
                        "function": "submit_contract",
                        "kwargs": {"name": name, "code": "@export\ndef value():\n    return 1"},
                        "nonce": VALID_NONCE,
                        "sender": SENDER,
                        "chi_supplied": 100,
                    }
                )
                response = await self.process_request(self.make_request(tx))
                self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
                self.assertIn("Transaction contract name is invalid", response.check_tx.log)
                self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_accepts_a_gap_without_cancelling_older_pending_nonces(self):
        committed_nonce = VALID_NONCE - 2
        self.app.nonce_storage.set_nonce(SENDER, committed_nonce)
        self.app.client.raw_driver.commit()
        lower = make_signed_tx_bytes(nonce=committed_nonce + 1)

        for raw in (VALID_TX_BYTES, lower):
            response = await self.process_request(self.make_request(raw))
            self.assertEqual(response.check_tx.code, self.constants.OkCode)
            self.assertEqual(self.app.nonce_storage.get_next_nonce(SENDER), committed_nonce + 1)

    async def test_check_tx_accepts_largest_wire_nonce(self):
        request = self.make_request(make_signed_tx_bytes(nonce=2**53 - 1))
        self.assertEqual((await self.process_request(request)).check_tx.code, self.constants.OkCode)

    async def test_check_tx_accepts_identical_retransmission(self):

        first_response = await self.process_request(self.make_request())
        second_response = await self.process_request(self.make_request())

        self.assertEqual(first_response.check_tx.code, self.constants.OkCode)
        self.assertEqual(second_response.check_tx.code, self.constants.OkCode)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejects_default_json_wire_format(
        self,
    ):
        tx_bytes = make_signed_tx_bytes_with_raw_spacing(nonce=VALID_NONCE)

        response = await self.process_request(self.make_request(tx_bytes))

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn("Transaction bytes are not canonical", response.check_tx.log)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejections_are_counted_not_logged_per_transaction(self):
        # A public RPC node can be sent any number of invalid transactions;
        # a WARNING record per rejection, which re-encoded the transaction to
        # hash it, turned that into log and CPU spam. Rejections are counted
        # and reported once per committed block; the responses are unchanged.
        rejected = [
            make_signed_tx_bytes(nonce=VALID_NONCE - 1),
            make_signed_tx_bytes(nonce=VALID_NONCE - 2),
            b"not hex",
        ]
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="INFO")
        self.addCleanup(logger.remove, sink)

        with patch("xian.app_logging.tx_hash_from_tx", side_effect=AssertionError):
            responses = [
                (await self.process_request(self.make_request(tx))).check_tx for tx in rejected
            ]

        self.assertEqual([response.code for response in responses], [1, 1, 1])
        self.assertIn("Transaction nonce is invalid", responses[0].log)
        self.assertEqual([record for record in records if record["level"].no >= 20], [])
        report_check_tx_rejections(self.app, height=7)
        report_check_tx_rejections(self.app, height=8)
        (summary,) = records
        self.assertEqual(summary["level"].name, "INFO")
        self.assertEqual(summary["extra"]["block_height"], 7)
        self.assertEqual(summary["extra"]["stage"], "check_tx")
        self.assertEqual(summary["extra"]["rejected_txs"], 3)
        self.assertEqual(summary["extra"]["TransactionException"], 3)

    async def test_check_tx_rejects_bad_signature(self):
        payload = {
            "chain_id": "xian-testnet-1",
            "contract": "currency",
            "function": "transfer",
            "kwargs": {"amount": 1, "to": SENDER},
            "nonce": VALID_NONCE,
            "sender": SENDER,
            "chi_supplied": 100,
        }
        tx_bytes = make_signed_tx_bytes_for_payload(payload)
        tx_hex = tx_bytes.decode("utf-8")
        tx_json = json.loads(bytes.fromhex(tx_hex).decode("utf-8"))
        tx_json["metadata"]["signature"] = "00" * 64
        tampered = encode_transaction_bytes(_canonical_json(tx_json))

        response = await self.process_request(self.make_request(tampered))

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn("Bad signature", response.check_tx.log)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejects_wrong_chain_id(self):
        payload = {
            "chain_id": "wrong-chain",
            "contract": "currency",
            "function": "transfer",
            "kwargs": {"amount": 1, "to": SENDER},
            "nonce": VALID_NONCE,
            "sender": SENDER,
            "chi_supplied": 100,
        }
        tx_bytes = make_signed_tx_bytes_for_payload(payload)

        response = await self.process_request(self.make_request(tx_bytes))

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn("Wrong chain_id", response.check_tx.log)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejects_tx_larger_than_configured_max_bytes(self):
        self.app.max_tx_bytes = len(VALID_TX_BYTES) - 1

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn("maximum configured size", response.check_tx.log)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejects_insufficient_chi(self):
        self.app.client.raw_driver.set(f"currency.balances:{SENDER}", 0)

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn(
            "Transaction sender's balance cannot cover the chi_supplied fee reservation",
            response.check_tx.log,
        )
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_accepts_a_balance_that_exactly_covers_the_reservation(self):
        # chi_supplied 100 at 20 chi per XIAN reserves exactly 5 XIAN.
        self.app.client.raw_driver.set(f"currency.balances:{SENDER}", 5)

        response = await self.process_request(
            self.make_request(make_signed_tx_bytes(nonce=VALID_NONCE, function="approve"))
        )

        self.assertEqual(response.check_tx.code, self.constants.OkCode)

    async def test_check_tx_transfer_must_fit_beside_the_fee_reservation(self):
        # Execution first reserves chi_supplied / chi rate (100 / 20 = 5) from
        # the balance, then the transfer spends what is left.
        balance, reservation = 100_000, 5
        cases = (
            ("transfer", balance + 1, False),
            ("transfer", balance - reservation + 1, False),
            ("transfer", balance - reservation, True),
            # Other currency functions do not spend the amount from the balance.
            ("approve", balance + 1, True),
        )
        for function, amount, accepted in cases:
            with self.subTest(function=function, amount=amount):
                response = await self.process_request(
                    self.make_request(
                        make_signed_tx_bytes(nonce=VALID_NONCE, amount=amount, function=function)
                    )
                )
                if accepted:
                    self.assertEqual(response.check_tx.code, self.constants.OkCode)
                    continue
                self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
                self.assertIn(
                    "currency.transfer amount plus the chi_supplied fee reservation "
                    "exceeds the sender's balance",
                    response.check_tx.log,
                )

    async def test_check_tx_free_metered_accepts_zero_balance_sender(self):
        self.app.tx_fee_policy = TxFeePolicy.free_metered(
            max_tx_chi=100,
            max_block_chi=1_000,
        )
        self.app.enable_tx_fee = False
        self.app.client.raw_driver.set(f"currency.balances:{SENDER}", 0)

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, self.constants.OkCode)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_applies_the_genesis_paid_policy_tx_cap(self):
        # The fixture genesis carries the default paid policy; its per-tx cap
        # holds even though the sender can afford a larger budget.
        self.assertEqual(self.app.tx_fee_policy.mode, "paid_metered")
        self.assertEqual(self.app.tx_fee_policy.max_tx_chi, DEFAULT_MAX_TX_CHI)

        over = await self.process_request(
            self.make_request(
                make_signed_tx_bytes(nonce=VALID_NONCE, chi_supplied=DEFAULT_MAX_TX_CHI + 1)
            )
        )
        at_cap = await self.process_request(
            self.make_request(
                make_signed_tx_bytes(nonce=VALID_NONCE, chi_supplied=DEFAULT_MAX_TX_CHI)
            )
        )

        self.assertEqual(over.check_tx.code, self.constants.ErrorCode)
        self.assertIn(f"per-transaction limit of {DEFAULT_MAX_TX_CHI} chi", over.check_tx.log)
        self.assertEqual(at_cap.check_tx.code, self.constants.OkCode)

    async def test_check_tx_rejects_tx_over_chi_cap_in_every_fee_mode(self):
        for factory in (TxFeePolicy.paid_metered, TxFeePolicy.free_metered):
            with self.subTest(mode=factory.__name__):
                self.app.tx_fee_policy = factory(max_tx_chi=99, max_block_chi=1_000)

                response = await self.process_request(self.make_request())

                self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
                self.assertIn(
                    "chi_supplied 100 exceeds the chain's per-transaction limit of 99 chi",
                    response.check_tx.log,
                )
                self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_accepts_runtime_big_int_transfer_amount(self):
        self.app.client.raw_driver.set(
            f"currency.balances:{SENDER}",
            2**65,
        )
        tx_bytes = make_signed_tx_bytes_for_payload(
            {
                "chain_id": "xian-testnet-1",
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"amount": {"__big_int__": str(2**64)}, "to": SENDER},
                "nonce": VALID_NONCE,
                "sender": SENDER,
                "chi_supplied": 100,
            }
        )

        wire = json.loads(bytes.fromhex(tx_bytes.decode()).decode())
        self.assertEqual(wire["payload"]["kwargs"]["amount"], {"__big_int__": str(2**64)})
        response = await self.process_request(self.make_request(tx_bytes))

        self.assertEqual(response.check_tx.code, self.constants.OkCode)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)

    async def test_check_tx_rejects_duplicate_payload_wire_format(self):
        malicious_tx = (
            '{"payload":{"chain_id":"xian-testnet-1","contract":"currency",'
            '"function":"transfer","kwargs":{"amount":999,"to":"'
            + SENDER
            + '"},"nonce":6,"sender":"'
            + SENDER
            + '","chi_supplied":100},"metadata":{"signature":"deadbeef"},'
            '"payload":{"chain_id":"xian-testnet-1","contract":"currency",'
            '"function":"transfer","kwargs":{"amount":1,"to":"'
            + SENDER
            + '"},"nonce":6,"sender":"'
            + SENDER
            + '","chi_supplied":100}}'
        )

        response = await self.process_request(
            self.make_request(encode_transaction_bytes(malicious_tx))
        )

        self.assertEqual(response.check_tx.code, self.constants.ErrorCode)
        self.assertIn("Transaction bytes are not canonical", response.check_tx.log)
        self.assertEqual(self.app.nonce_storage.get_nonce(SENDER), VALID_NONCE - 1)


if __name__ == "__main__":
    unittest.main()
