"""The Rust block executor reads a transaction's execution inputs from its
wire bytes; they must equal what the Python node built per transaction.

Reference model (the code the executor replaced): the kwargs
``coerce_call_kwargs(decode_wire_numbers(kwargs))`` at the V2 VM boundary,
the context ``TxProcessor.get_environment`` + ``_vm_context`` gave, the
canonical size ``canonical_transaction_size_bytes`` and ``chi_supplied``.
A transaction whose Python inputs did not convert runs on the Python path
(``None`` on both sides).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from admission_corpus import CHAIN_ID, cases, node_admission
from xian_runtime_types.encoding import decode_wire_numbers

from xian.processor import TxProcessor
from xian.utils.tx import canonical_transaction_size_bytes, transaction_bytes

xian_vm_core = pytest.importorskip("xian_vm_core")
from xian_vm_core import coerce_call_kwargs  # noqa: E402
from xian_vm_core._native import _transaction_inputs_debug, _vm_value_debug  # noqa: E402

BLOCK_METAS = [
    {"nanos": 1_700_000_000_123_456_789, "height": 7, "hash": "ab" * 32, "chain_id": CHAIN_ID},
    {"nanos": 1_000_000_000, "height": 1, "hash": "00" * 32, "chain_id": CHAIN_ID},
    {"nanos": 4_102_444_799_999_999_999, "height": 2**40, "hash": "ff" * 32, "chain_id": "other"},
]


FIELDS = ("sender", "contract", "function", "chi_budget", "size_bytes", "kwargs", "context")


def _rust_inputs(raw: bytes, meta: dict):
    inputs = _transaction_inputs_debug(raw, meta)
    return None if inputs is None else dict(zip(FIELDS, inputs, strict=True))


def _environment(tx: dict) -> dict:
    processor = object.__new__(TxProcessor)
    processor.execution_runtime = SimpleNamespace(mode="xian_vm_v2")
    return processor.get_environment(tx)


def _python_inputs(tx: dict):
    payload = tx["payload"]
    try:
        kwargs = coerce_call_kwargs(decode_wire_numbers(payload["kwargs"]))
        rendered = [(key, _vm_value_debug(value)) for key, value in kwargs.items()]
    except Exception:  # noqa: BLE001 - the Python path ran these
        return None
    name = kwargs.get("name") if payload["contract"] == "submission" else None
    if name is not None and not isinstance(name, str):
        return None
    environment = _environment(tx)
    return {
        "sender": payload["sender"],
        "contract": payload["contract"],
        "function": payload["function"],
        "chi_budget": payload["chi_supplied"],
        "size_bytes": canonical_transaction_size_bytes(tx),
        "kwargs": rendered,
        "context": {
            "signer": payload["sender"],
            "caller": payload["sender"],
            "this": payload["contract"],
            "entry": (payload["contract"], payload["function"]),
            "submission_name": name,
            "now": environment["now"],
            "block_num": environment["block_num"],
            "block_hash": environment["block_hash"],
            "chain_id": environment["chain_id"],
            "__input_hash": environment["__input_hash"],
        },
    }


def _admitted() -> list[tuple[str, bytes, dict]]:
    admitted = []
    for name, raw in cases():
        try:
            admitted.append((name, raw, node_admission(raw)))
        except Exception:  # noqa: BLE001 - refused transactions never execute
            continue
    return admitted


ADMITTED = _admitted()


def _system_txs() -> list[dict]:
    return [
        {
            "payload": {
                "sender": "sys",
                "contract": "validators",
                "function": "rebalance",
                "kwargs": {},
                "chi_supplied": 0,
            },
            "metadata": {"signature": "validator-epoch-rebalance:9"},
        },
        {
            "payload": {
                "sender": "sys",
                "contract": "validators",
                "function": "apply_evidence_penalty",
                "kwargs": {
                    "member": "ab" * 32,
                    "infraction_type": "duplicate_vote",
                    "evidence_id": "é" * 3,
                    "evidence_height": 12,
                },
                "chi_supplied": 0,
            },
            "metadata": {"signature": "evidence-penalty:x"},
        },
    ]


def test_the_corpus_reaches_every_kind_of_input():
    assert len(ADMITTED) > 300


@pytest.mark.parametrize("meta", BLOCK_METAS, ids=lambda meta: str(meta["height"]))
def test_rust_reads_the_inputs_the_python_node_built(meta):
    mismatches = {}
    for name, raw, tx in ADMITTED:
        expected = _python_inputs({**tx, "b_meta": meta})
        actual = _rust_inputs(raw, meta)
        if actual != expected:
            mismatches[name] = (actual, expected)
    assert not mismatches, mismatches


@pytest.mark.parametrize("meta", BLOCK_METAS, ids=lambda meta: str(meta["height"]))
def test_system_transactions_read_from_their_canonical_bytes(meta):
    # The wire form sorts the keyword arguments by name; the VM binds them by
    # name (the system corpus in engine_parity checks the receipts).
    for tx in _system_txs():
        raw = transaction_bytes(tx)
        canonical = json.loads(bytes.fromhex(raw.decode()))
        expected = _python_inputs({**canonical, "b_meta": meta})
        assert expected is not None
        assert _rust_inputs(raw, meta) == expected
