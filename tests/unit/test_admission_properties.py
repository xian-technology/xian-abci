"""Properties of static admission over mutated signed transactions.

FinalizeBlock admits every block transaction, then hands the admitted bytes
to the Rust block executor, which raises (and so halts the node) for bytes it
cannot read. Over seeded byte- and text-level mutations of signed
transactions these must hold:

- admission refuses with ``ValueError`` only (a panic would escape the
  node's ``except Exception``);
- an admitted transaction is spelled exactly as its canonical JSON in
  lowercase hex, so one signed document has one CometBFT hash;
- the block executor reads every admitted transaction.
"""

from __future__ import annotations

import json
import random

import pytest
from admission_corpus import CHAIN_ID, base_payload, canonical, hex_bytes, signed_tx

xian_vm_core = pytest.importorskip("xian_vm_core")
from xian_vm_core import decode_and_validate_transaction  # noqa: E402
from xian_vm_core._native import _transaction_inputs_debug  # noqa: E402

SEED = 20261115
MUTATIONS = 20_000
BLOCK_META = {
    "nanos": 1_700_000_000_123_456_789,
    "height": 7,
    "hash": "ab" * 32,
    "chain_id": CHAIN_ID,
}
SEED_KWARGS = (
    {"amount": 5, "to": "bob"},
    {"amount": {"__fixed__": "1.5"}, "memo": "é😀\u2028"},
    {"a": {"__big_int__": "9007199254740992"}, "b": [1, [2, {"c": None}], True]},
    {"a": {"__set__": {"codec": "xian-set-1", "items": [["int", "1"], ["str", "a"]]}}},
    {"a": {"__time__": [2024, 1, 2, 3, 4, 5, 6]}, "b": {"__delta__": [1, 2]}},
    {"a": {"__bytes__": "00ff"}, "b": {"\uffff": 1, "😀": 2, "Z": 3}},
)
TOKENS = (
    '"',
    "{",
    "}",
    "[",
    "]",
    ",",
    ":",
    "0",
    "-",
    "1e5",
    "1.0",
    "\\u0000",
    "\\ud800",
    "null",
    "true",
    "9999999999999999999999",
    '"__fixed__"',
    '"__set__"',
    " ",
    "é",
    '\\"',
)


def _mutated_wire(base: bytes, rng: random.Random) -> bytes:
    if rng.random() < 0.4:
        raw = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            raw[rng.randrange(len(raw))] = rng.choice(b"0123456789abcdefABCDEF")
        return bytes(raw)
    text = list(bytes.fromhex(base.decode()).decode())
    for _ in range(rng.randint(1, 3)):
        at = rng.randrange(len(text))
        operation = rng.randrange(3)
        if operation == 0:
            text[at] = rng.choice(TOKENS)
        elif operation == 1:
            text.insert(at, rng.choice(TOKENS))
        else:
            del text[at]
    return "".join(text).encode("utf-8", "surrogatepass").hex().encode()


def _admitted(raw: bytes):
    try:
        return decode_and_validate_transaction(raw, CHAIN_ID)
    except ValueError:
        return None


def test_mutated_transactions_are_refused_or_admitted_in_one_readable_spelling():
    rng = random.Random(SEED)
    seeds = [signed_tx(base_payload(kwargs=kwargs)) for kwargs in SEED_KWARGS]
    admitted = 0
    for _ in range(MUTATIONS):
        raw = _mutated_wire(rng.choice(seeds), rng)
        document = _admitted(raw)
        if document is None:
            continue
        admitted += 1
        assert hex_bytes(canonical(document)) == raw
        assert _transaction_inputs_debug(raw, BLOCK_META) is not None
    assert admitted > 0


def test_every_seed_is_admitted_and_its_uppercase_spelling_is_not():
    for kwargs in SEED_KWARGS:
        raw = signed_tx(base_payload(kwargs=kwargs))
        assert json.loads(canonical(_admitted(raw))) == json.loads(bytes.fromhex(raw.decode()))
        assert _admitted(raw.upper()) is None
