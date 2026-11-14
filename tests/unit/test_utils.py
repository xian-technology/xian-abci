import decimal
import json
import unittest

import nacl.encoding
import nacl.signing
from parameterized import parameterized
from xian_runtime_types.decimal import ContractingDecimal

import xian.utils.encoding as encoding
from xian.utils.encoding import (
    decode_transaction_bytes,
    encode_abci_json,
    encode_transaction_bytes,
    normalize_for_abci_json,
    stringify_decimals,
)
from xian.utils.tx import (
    canonical_json,
    canonical_transaction_size_bytes,
    format_dictionary,
)

SEED = bytes(range(32))
SENDER = (
    nacl.signing.SigningKey(SEED)
    .verify_key.encode(encoder=nacl.encoding.HexEncoder)
    .decode("ascii")
)


def _canonical_json(value: dict) -> str:
    return canonical_json(value)


def _signed_transaction_json(*, mutate_signature: bool = False) -> str:
    payload = {
        "chain_id": "xian-local",
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"memo": "snowman: \u2603", "to": "UNICODE_TEST"},
        "nonce": 40,
        "sender": SENDER,
        "chi_supplied": 10,
    }
    signing_key = nacl.signing.SigningKey(SEED)
    signature = signing_key.sign(_canonical_json(payload).encode("utf-8")).signature.hex()
    if mutate_signature:
        signature = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    tx = {
        "metadata": {"signature": signature},
        "payload": format_dictionary(payload),
    }
    return _canonical_json(tx)


class TestEncoding(unittest.TestCase):
    @parameterized.expand(
        [
            (
                "valid_transaction",
                _signed_transaction_json(),
                False,
                None,
            ),
            (
                "default_json_spacing",
                json.dumps(json.loads(_signed_transaction_json())),
                True,
                "Transaction bytes are not canonical",
            ),
            (
                "multiple_payload_fields",
                '{"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":10000000000,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}, "metadata":{"signature":"847871676c33d17d5a86bd8b2f12832e35e2b73692b0f28321be2f9acd3379c755440333ddc5e5bf40255256adb946aecae6729e8cb3a9028b08cdd995609f05"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
                "Transaction bytes are not canonical",
            ),
        ]
    )
    def test_decode_transaction_bytes(self, name, tx_str, should_raise, expected_error):
        tx_bytes = encode_transaction_bytes(tx_str)
        if should_raise:
            with self.assertRaises(ValueError) as context:
                tx_json_decoded, payload_str = decode_transaction_bytes(tx_bytes)
            self.assertIn(expected_error, str(context.exception))
        else:
            tx_json_decoded, payload_str = decode_transaction_bytes(tx_bytes)
        # self.assertEqual(tx_json_decoded, tx_json)


class TestAbciJsonDecimalFormatting(unittest.TestCase):
    def test_canonical_json_rejects_excessive_depth(self):
        value = "leaf"
        for _ in range(encoding.MAX_CANONICAL_JSON_DEPTH + 1):
            value = {"next": value}

        with self.assertRaisesRegex(ValueError, "recursion limit exceeded"):
            encoding.canonical_json_text(value)

    def test_stringify_decimals_uses_plain_strings(self):
        value = {
            "amount": ContractingDecimal("5000"),
            "fees": [decimal.Decimal("1.2500"), decimal.Decimal("0.000005")],
        }

        self.assertEqual(
            stringify_decimals(value),
            {"amount": "5000", "fees": ["1.25", "0.000005"]},
        )

    def test_stringify_decimals_spells_decimals_like_the_wire_format(self):
        # Event attributes use the same plain spelling as {"__fixed__": ...} values.
        cases = [
            (ContractingDecimal("1000"), "1000"),
            (ContractingDecimal("-2500000"), "-2500000"),
            (ContractingDecimal("0.0000001"), "0.0000001"),
            (ContractingDecimal("1e-30"), "0.000000000000000000000000000001"),
            (ContractingDecimal("9" * 61 + "." + "9" * 30), "9" * 61 + "." + "9" * 30),
            (ContractingDecimal("0"), "0"),
            (decimal.Decimal("5E+3"), "5000"),
            (decimal.Decimal("1.2500"), "1.25"),
            (decimal.Decimal("-0.00"), "0"),
            (decimal.Decimal("-0"), "0"),
            (decimal.Decimal("-12.340"), "-12.34"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(stringify_decimals(value), expected)
                self.assertEqual(normalize_for_abci_json(value), {"__fixed__": expected})

    def test_stringify_decimals_renders_event_values_as_text(self):
        from xian_runtime_types.time import Datetime

        self.assertEqual(
            stringify_decimals(
                {
                    "when": Datetime(2026, 11, 13, 1, 2, 3),
                    "memo": "caf\u00e9".encode(),
                    "raw": b"\xff",
                    "count": 7,
                }
            ),
            {"when": "2026-11-13 01:02:03", "memo": "caf\u00e9", "raw": "b'\\xff'", "count": 7},
        )

    def test_stringify_decimals_blanks_values_without_a_wire_spelling(self):
        # Outside the 61.30 range there is no {"__fixed__"} spelling either.
        self.assertEqual(stringify_decimals(decimal.Decimal("1E+100")), "")
        self.assertEqual(stringify_decimals([decimal.Decimal("1E-31")]), [""])

    def test_normalize_for_abci_json_uses_canonical_numeric_tags(self):
        value = {
            "amount": decimal.Decimal("5E+3"),
            "nested": {
                "small": decimal.Decimal("0.000005"),
                "zero": decimal.Decimal("-0"),
            },
        }

        self.assertEqual(
            normalize_for_abci_json(value),
            {
                "amount": {"__fixed__": "5000"},
                "nested": {"small": {"__fixed__": "0.000005"}, "zero": {"__fixed__": "0"}},
            },
        )

    def test_normalize_for_abci_json_tags_timedeltas_and_bytearrays(self):
        from xian_runtime_types.time import Timedelta

        # A contract may store, return and emit these; receipts and state-root
        # leaves need a JSON form for them, the wire form the encoder writes.
        value = {
            "span": Timedelta(days=-3, seconds=5),
            "raw": bytearray(b"\x00\xff"),
            "nested": [(Timedelta(days=999_999_999, seconds=86_399), bytearray())],
        }

        self.assertEqual(
            encode_abci_json(value),
            b'{"nested":[[{"__delta__":[999999999,86399]},{"__bytearray__":""}]],'
            b'"raw":{"__bytearray__":"00ff"},"span":{"__delta__":[-3,5]}}',
        )

    def test_normalize_for_abci_json_preserves_sorted_keys(self):
        value = {
            "z_amount": decimal.Decimal("5E+3"),
            "a_amount": ContractingDecimal("10.5000"),
        }

        self.assertEqual(
            list(normalize_for_abci_json(value).items()),
            [("a_amount", {"__fixed__": "10.5"}), ("z_amount", {"__fixed__": "5000"})],
        )


class TestTransactionSizing(unittest.TestCase):
    def test_canonical_transaction_size_bytes_is_stable_across_key_order(self):
        tx_a = {
            "payload": {
                "sender": "abc",
                "nonce": 1,
                "chi_supplied": 10,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": "bob", "amount": 5},
                "chain_id": "xian-local",
            },
            "metadata": {"signature": "deadbeef"},
        }
        tx_b = {
            "metadata": {"signature": "deadbeef"},
            "payload": {
                "function": "transfer",
                "contract": "currency",
                "kwargs": {"amount": 5, "to": "bob"},
                "chi_supplied": 10,
                "chain_id": "xian-local",
                "nonce": 1,
                "sender": "abc",
            },
        }

        self.assertEqual(
            canonical_transaction_size_bytes(tx_a),
            canonical_transaction_size_bytes(tx_b),
        )

    def test_canonical_transaction_size_bytes_ignores_node_added_block_meta(
        self,
    ):
        tx = {
            "payload": {
                "sender": "abc",
                "nonce": 1,
                "chi_supplied": 10,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": "bob", "amount": 5},
                "chain_id": "xian-local",
            },
            "metadata": {"signature": "deadbeef"},
        }

        sized_with_block_meta = dict(tx)
        sized_with_block_meta["b_meta"] = {
            "height": 5,
            "hash": "block-hash",
        }

        self.assertEqual(
            canonical_transaction_size_bytes(tx),
            canonical_transaction_size_bytes(sized_with_block_meta),
        )


if __name__ == "__main__":
    unittest.main()
