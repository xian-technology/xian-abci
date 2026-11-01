"""Already encoded transaction envelopes must not be validated as runtime values."""

import hashlib
import json
from copy import deepcopy

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.utils.tx import canonical_transaction_size_bytes, tx_hash_from_tx

# Raw quantum, large integer text, nested tags and escaped Unicode are deliberate:
# decoding/re-encoding the wire tree would silently change some of these bytes.
WIRE = {
    "payload": {
        "sender": "alice",
        "kwargs": {
            "amount": {"__fixed__": "0.00100"},
            "nested": [{"fee": {"__fixed__": "2.00"}}, {"__big_int__": "0018446744073709551616"}],
            "label": "é",
        },
        "function": "settle",
        "contract": "con_x402",
    },
    "metadata": {"signature": "sig"},
}
GOLDEN = (
    '{"metadata":{"signature":"sig"},"payload":{"contract":"con_x402",'
    '"function":"settle","kwargs":{"amount":{"__fixed__":"0.00100"},'
    '"label":"\\u00e9","nested":[{"fee":{"__fixed__":"2.00"}},'
    '{"__big_int__":"0018446744073709551616"}]},"sender":"alice"}}'
).encode()


def test_wire_tags_keep_exact_canonical_bytes_for_size_and_hash():
    original = deepcopy(WIRE)
    assert canonical_transaction_size_bytes(WIRE) == len(GOLDEN)
    assert tx_hash_from_tx(WIRE) == hashlib.sha3_256(GOLDEN).hexdigest()
    assert WIRE == original


def test_only_size_excludes_node_added_fields_hash_keeps_historical_input():
    transaction = {
        **deepcopy(WIRE),
        "b_meta": {"height": 3},
        "runtime_extra": {"__fixed__": "1.00"},
    }
    assert canonical_transaction_size_bytes(transaction) == len(GOLDEN)
    full = b'{"b_meta":{"height":3},' + GOLDEN[1:-1] + b',"runtime_extra":{"__fixed__":"1.00"}}'
    assert tx_hash_from_tx(transaction) == hashlib.sha3_256(full).hexdigest()
    assert tx_hash_from_tx(transaction) != tx_hash_from_tx(WIRE)


def test_typed_callers_and_bigint_normalization_keep_prior_canonical_encoding():
    transaction = {
        "metadata": {},
        "payload": {
            "kwargs": {
                "amount": ContractingDecimal("0.001"),
                "bytes": b"\x00\xff",
                "count": 2**80,
                "flag": True,
                "when": Datetime(2026, 11, 1),
            }
        },
    }
    expected = (
        '{"metadata":{},"payload":{"kwargs":{"amount":{"__fixed__":"0.001"},'
        '"bytes":{"__bytes__":"00ff"},"count":{"__big_int__":"1208925819614629174706176"},'
        '"flag":true,"when":{"__time__":[2026,11,1,0,0,0,0]}}}}'
    ).encode()
    assert canonical_transaction_size_bytes(transaction) == len(expected)
    assert tx_hash_from_tx(transaction) == hashlib.sha3_256(expected).hexdigest()


def test_persistent_reserved_tag_guard_remains_strict():
    with pytest.raises(ValueError, match="reserved runtime codec dictionary key"):
        encode(WIRE)


def test_size_and_hash_are_stable_when_wire_keys_arrive_in_another_order():
    alternate = json.loads(GOLDEN)
    assert canonical_transaction_size_bytes(alternate) == canonical_transaction_size_bytes(WIRE)
    assert tx_hash_from_tx(alternate) == tx_hash_from_tx(WIRE)
