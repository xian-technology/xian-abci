"""Already encoded transaction envelopes must not be validated as runtime values."""

import hashlib
import json
from copy import deepcopy

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.utils.tx import canonical_transaction_size_bytes, tx_hash_from_tx

# Canonical numeric wrappers and escaped Unicode must hash to these exact bytes.
WIRE = {
    "payload": {
        "sender": "alice",
        "kwargs": {
            "amount": {"__fixed__": "0.001"},
            "nested": [{"fee": {"__fixed__": "2"}}, {"__big_int__": "18446744073709551616"}],
            "label": "é",
        },
        "function": "settle",
        "contract": "con_x402",
    },
    "metadata": {"signature": "sig"},
}
GOLDEN = (
    '{"metadata":{"signature":"sig"},"payload":{"contract":"con_x402",'
    '"function":"settle","kwargs":{"amount":{"__fixed__":"0.001"},'
    '"label":"\\u00e9","nested":[{"fee":{"__fixed__":"2"}},'
    '{"__big_int__":"18446744073709551616"}]},"sender":"alice"}}'
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
        "runtime_extra": {"__fixed__": "1"},
    }
    assert canonical_transaction_size_bytes(transaction) == len(GOLDEN)
    full = b'{"b_meta":{"height":3},' + GOLDEN[1:-1] + b',"runtime_extra":{"__fixed__":"1"}}'
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


@pytest.mark.parametrize(
    "value", [{"__fixed__": "0.00100"}, {"__big_int__": "0018446744073709551616"}, 0.5]
)
def test_nested_noncanonical_numbers_cannot_be_hashed(value):
    wire = deepcopy(WIRE)
    wire["payload"]["kwargs"]["nested"].append(value)
    with pytest.raises((TypeError, ValueError)):
        tx_hash_from_tx(wire)


def test_node_accepts_shared_signed_numeric_vectors():
    from pathlib import Path

    from xian.utils import tx

    fixture = Path(__file__).resolve().parents[3] / "xian-contracting/tests/fixtures/transaction_numeric_v2.json"
    for case in json.loads(fixture.read_text())["cases"]:
        raw = case["transaction_json"].encode().hex().encode()
        admitted = tx.decode_and_validate_transaction_static_bytes(
            raw, chain_id=case["payload"]["chain_id"]
        )
        assert admitted == {"payload": case["payload"], "metadata": {"signature": case["signature"]}}


@pytest.mark.parametrize(
    "token",
    ["-0", "1.0000000000000001", "1e-400", "9007199254740992",
     '{"__fixed__":"1.0"}', '{"__fixed__":"' + "9" * 62 + '"}'],
)
def test_signed_invalid_numeric_tokens_are_rejected(token):
    from nacl.signing import SigningKey

    from xian.utils import tx

    signer = SigningKey(bytes(32))
    payload = {
        "chain_id": "local", "contract": "con_probe", "function": "call",
        "kwargs": {"nested": ["TOKEN"]}, "nonce": 0, "chi_supplied": 50,
        "sender": signer.verify_key.encode().hex(),
    }
    message = tx.canonical_json(payload).replace('"TOKEN"', token)
    signature = signer.sign(message.encode()).signature.hex()
    raw = ('{"metadata":{"signature":"' + signature + '"},"payload":' + message + '}').encode()
    with pytest.raises(tx.TransactionException):
        tx.decode_and_validate_transaction_static_bytes(raw.hex().encode(), chain_id="local")
