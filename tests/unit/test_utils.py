import decimal
import json
import unittest

from parameterized import parameterized
from xian_runtime_types.decimal import ContractingDecimal

from xian.utils.encoding import (
    decode_transaction_bytes,
    encode_transaction_bytes,
    extract_payload_string,
    normalize_for_abci_json,
    stringify_decimals,
)
from xian.utils.tx import (
    canonical_transaction_size_bytes,
    unpack_transaction,
    verify,
)


class TestPayloadStrExtraction(unittest.TestCase):

    @parameterized.expand(
        [
            (
                "preserve_payload_as_string",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
            (
                "preserve_payload_with_nested_json_as_string",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST","nested_data":{"key1":"value1","key2":{"subkey":"subvalue"}}},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
            (
                "preserve_payload_with_deeply_nested_json_as_string",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST","nested_data":{"key1":"value1","key2":{"subkey":"subvalue","deeper":{"deep_key":"deep_value","deep_array":[{"array_key1":"array_value1"},{"array_key2":"array_value2"}]}}}},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
            (
                "bracket_in_payload_string",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST}"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
            (
                "double_slash_escape_in_payload",
                '{"metadata":{"signature":"abc"},"payload":{"text":"This is a \\"quoted\\" string","number":123}}',
                True,
            ),
            (
                "unicode_escapes_in_payload",
                '{"metadata":{"signature":"abc"},"payload":{"text":"Unicode test: \\u2603 \\u2764"}}',
                True,
            ),
            ("no_payload_field", '{"id": 2, "other_field": "data"}', False),
            (
                "empty_payload",
                '{"id": 3, "payload": "", "other_field": "data"}',
                False,
            ),
            (
                "escaped_quotes_in_payload",
                '{"metadata":{"signature":"abc"},"payload":{"text":"This is a \\"quoted\\" string","number":123}}',
                True,
            ),
            (
                "special_characters_in_payload",
                '{"metadata":{"signature":"abc"},"payload":{"text":"Special characters !@#$%^&*()_+-=~`"}}',
                True,
            ),
            (
                "payload_with_empty_object",
                '{"metadata":{"signature":"abc"},"payload":{}}',
                True,
            ),
            (
                "payload_with_empty_array",
                '{"metadata":{"signature":"abc"},"payload":{"array":[]}}',
                True,
            ),
            (
                "payload_with_large_numbers",
                '{"metadata":{"signature":"abc"},"payload":{"large_number":12345678901234567890}}',
                True,
            ),
            (
                "payload_with_unicode_characters",
                '{"metadata":{"signature":"abc"},"payload":{"text":"Unicode test: \u2603 \u2764"}}',
                True,
            ),
            (
                "payload_with_boolean_values",
                '{"metadata":{"signature":"abc"},"payload":{"flag":true,"status":false}}',
                True,
            ),
            (
                "payload_with_null_value",
                '{"metadata":{"signature":"abc"},"payload":{"nullable":null}}',
                True,
            ),
            (
                "double-slash-escape-in-payload",
                '{"metadata":{"signature":"abc"},"payload":{"text":"This is a \\" } quoted\\" string","number":123}}',
                True,
            ),
        ]
    )
    def test_extract_payload(
        self, name, tx_str, has_payload, should_match=True
    ):
        complete_json = json.loads(tx_str)
        if has_payload:
            result = json.loads(extract_payload_string(tx_str))
            if should_match:
                self.assertEqual(result, complete_json["payload"])
            else:
                self.assertNotEqual(result, complete_json["payload"])
        else:
            with self.assertRaises(ValueError):
                extract_payload_string(tx_str)


class TestVerification(unittest.TestCase):
    @parameterized.expand(
        [
            (
                "valid_transaction",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
            (
                "invalid_signature",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550c"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                False,
            ),
        ]
    )
    def test_verify(self, name, tx_str, expected_result):
        tx_json = json.loads(tx_str)
        payload_str = extract_payload_string(tx_str)
        sender, signature, payload = unpack_transaction(tx_json)
        self.assertEqual(
            verify(sender, payload_str, signature), expected_result
        )


class TestEncoding(unittest.TestCase):
    @parameterized.expand(
        [
            (
                "valid_transaction",
                '{"metadata":{"signature":"7ef14c974af43f9a2b2ebb17cfff96615571094f427b29f766e38394cf7ad8ea92c5d645eab3d8ed820d4ad93af7d57a10ed56d6d5f6b96f0094996c1f5a550d"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                False,
            ),
            (
                "multiple_payload_fields",
                '{"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":10000000000,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}, "metadata":{"signature":"847871676c33d17d5a86bd8b2f12832e35e2b73692b0f28321be2f9acd3379c755440333ddc5e5bf40255256adb946aecae6729e8cb3a9028b08cdd995609f05"},"payload":{"chain_id":"xian-local","contract":"currency","function":"transfer","kwargs":{"amount":0.00000252,"to":"JAVASCRIPT_TRANSACTION_TEST"},"nonce":40,"sender":"d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737","chi_supplied":10}}',
                True,
            ),
        ]
    )
    def test_decode_transaction_bytes(self, name, tx_str, should_raise):
        tx_bytes = encode_transaction_bytes(tx_str)
        if should_raise:
            with self.assertRaises(ValueError) as context:
                tx_json_decoded, payload_str = decode_transaction_bytes(
                    tx_bytes
                )
            self.assertTrue("Invalid payload" in str(context.exception))
        else:
            tx_json_decoded, payload_str = decode_transaction_bytes(tx_bytes)
        # self.assertEqual(tx_json_decoded, tx_json)


class TestAbciJsonDecimalFormatting(unittest.TestCase):
    def test_stringify_decimals_uses_plain_strings(self):
        value = {
            "amount": ContractingDecimal("5000"),
            "fees": [decimal.Decimal("1.2500"), decimal.Decimal("0.000005")],
        }

        self.assertEqual(
            stringify_decimals(value),
            {"amount": "5000", "fees": ["1.25", "0.000005"]},
        )

    def test_normalize_for_abci_json_uses_plain_strings(self):
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
                "amount": "5000",
                "nested": {"small": "0.000005", "zero": "0"},
            },
        )

    def test_normalize_for_abci_json_preserves_sorted_keys(self):
        value = {
            "z_amount": decimal.Decimal("5E+3"),
            "a_amount": ContractingDecimal("10.5000"),
        }

        self.assertEqual(
            list(normalize_for_abci_json(value).items()),
            [("a_amount", "10.5"), ("z_amount", "5000")],
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

    def test_canonical_transaction_size_bytes_ignores_node_added_block_meta(self):
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
