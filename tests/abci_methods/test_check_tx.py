import logging
import json
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
from cometbft.abci.v1beta1.types_pb2 import RequestCheckTx
from cometbft.abci.v1beta3.types_pb2 import Request, Response
from xian.constants import Constants as c
from xian.utils.encoding import encode_transaction_bytes
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
    return encode(decode(encode(_sort_keys_deep(payload))))


def make_signed_tx_bytes(*, nonce: int) -> bytes:
    return make_signed_tx_bytes_for_payload(
        {
            "chain_id": "xian-testnet-1",
            "contract": "currency",
            "function": "transfer",
            "kwargs": {"amount": 1, "to": SENDER},
            "nonce": nonce,
            "sender": SENDER,
            "chi_supplied": 100,
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


SENDER = nacl.signing.SigningKey(SEED).verify_key.encode(
    encoder=nacl.encoding.HexEncoder
).decode("ascii")
VALID_TX_BYTES = make_signed_tx_bytes(nonce=VALID_NONCE)


async def deserialize(raw: bytes) -> Response:
    return next(read_messages(BytesIO(raw), Response))


class TestCheckTx(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup_fixtures()
        self.app = await Xian.create(constants=MockConstants)
        self.app.current_block_meta = {"height": 0, "nanos": 0}
        self.app.chain_id = "xian-testnet-1"
        self.app.client.raw_driver.set(f"currency.balances:{SENDER}", 100000)
        self.handler = ProtocolHandler(self.app)

    async def asyncTearDown(self):
        teardown_fixtures()

    async def process_request(self, req):
        raw = await self.handler.process("check_tx", req)
        return await deserialize(raw)

    def make_request(self, tx_bytes=VALID_TX_BYTES):
        return Request(check_tx=RequestCheckTx(tx=tx_bytes))

    async def test_check_tx_accepts_next_nonce(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, c.OkCode)
        self.assertEqual(
            self.app.nonce_storage.get_pending_nonce(SENDER),
            VALID_NONCE,
        )

    async def test_check_tx_rejects_skipped_nonce(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 2)

        response = await self.process_request(self.make_request())

        self.assertEqual(response.check_tx.code, c.ErrorCode)
        self.assertIn("Expected 5, got 6", response.check_tx.log)
        self.assertIsNone(self.app.nonce_storage.get_pending_nonce(SENDER))

    async def test_check_tx_advances_pending_nonce_for_sequenced_txs(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
        next_tx = make_signed_tx_bytes(nonce=VALID_NONCE + 1)

        first_response = await self.process_request(self.make_request())
        second_response = await self.process_request(self.make_request(next_tx))

        self.assertEqual(first_response.check_tx.code, c.OkCode)
        self.assertEqual(second_response.check_tx.code, c.OkCode)
        self.assertEqual(
            self.app.nonce_storage.get_pending_nonce(SENDER),
            VALID_NONCE + 1,
        )

    async def test_check_tx_accepts_identical_retransmission(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)

        first_response = await self.process_request(self.make_request())
        second_response = await self.process_request(self.make_request())

        self.assertEqual(first_response.check_tx.code, c.OkCode)
        self.assertEqual(second_response.check_tx.code, c.OkCode)
        self.assertEqual(
            self.app.nonce_storage.get_pending_nonce(SENDER),
            VALID_NONCE,
        )

    async def test_check_tx_accepts_canonical_signature_with_default_json_wire_format(
        self,
    ):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
        tx_bytes = make_signed_tx_bytes_with_raw_spacing(nonce=VALID_NONCE)

        response = await self.process_request(self.make_request(tx_bytes))

        self.assertEqual(response.check_tx.code, c.OkCode)

    async def test_check_tx_expires_stale_pending_reservations(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)

        response = await self.process_request(self.make_request())
        self.assertEqual(response.check_tx.code, c.OkCode)
        reservation = self.app.nonce_storage.pending_nonces[SENDER][VALID_NONCE]

        with patch.object(
            self.app.nonce_storage,
            "_now",
            return_value=(
                reservation.reserved_at
                + self.app.nonce_storage.reservation_ttl_seconds
                + 1.0
            ),
        ):
            self.assertEqual(
                self.app.nonce_storage.get_next_nonce(SENDER),
                VALID_NONCE,
            )

    async def test_check_tx_bad_signature_does_not_reserve_nonce(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
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

        self.assertEqual(response.check_tx.code, c.ErrorCode)
        self.assertIn("Bad signature", response.check_tx.log)
        self.assertIsNone(self.app.nonce_storage.get_pending_nonce(SENDER))

    async def test_check_tx_wrong_chain_id_does_not_reserve_nonce(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
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

        self.assertEqual(response.check_tx.code, c.ErrorCode)
        self.assertIn("Wrong chain_id", response.check_tx.log)
        self.assertIsNone(self.app.nonce_storage.get_pending_nonce(SENDER))

    async def test_check_tx_rejects_duplicate_payload_wire_format(self):
        self.app.nonce_storage.set_nonce(SENDER, VALID_NONCE - 1)
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

        self.assertEqual(response.check_tx.code, c.ErrorCode)
        self.assertIn("Invalid payload", response.check_tx.log)
        self.assertIsNone(self.app.nonce_storage.get_pending_nonce(SENDER))


if __name__ == "__main__":
    unittest.main()
