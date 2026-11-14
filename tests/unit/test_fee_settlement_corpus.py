"""Pin fee reservation, refunds and failure charges over many inputs.

Step B moves fee settlement into Rust; the port must reproduce these outputs
exactly (exact-ratio reservation check, ContractingDecimal truncation, the
integer 0 that ``max(balance - fee, 0)`` yields for a negative balance, and
the chi a failure paid: ``chi_used``, or the whole balance priced in chi).
``tests/fixtures/fee_outputs.json`` holds what the Python reference (deleted
in increment 9 of the single-engine plan) computed, encoded as wire JSON.
The Rust port must answer every pinned case identically, except for
execution revision 14's rule that a failure charge leaves a negative balance
as it is (Python raised it to 0, minting the difference). On a wider corpus
with odd value types it refuses a balance that is not a number (``None``:
such a sender cannot pay); ``fee_outputs_wide.json`` freezes those outcomes.
The fixtures are not regenerated.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fee_outputs.json"
# The wide corpus's Rust outcomes (None where Rust refuses a balance), frozen
# where they matched the Python reference before it was deleted.
WIDE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fee_outputs_wide.json"
SEED = 20261113
CASES = 400
_ABSENT = object()

BALANCES = [
    None,
    0,
    1,
    7,
    10**6,
    10**60,
    "0.05",
    "33.333333333333333333333333333334",
    "33.333333333333333333333333333333",
    "-1",
    "123456.789",
    "0.000000000000000000000000000001",
    "9" * 61,
]
BUDGETS = [0, 1, 2, 99, 100, 213, 200_000, -3]
RATES = [1, 3, 7, 20, 2000]
CHI_USED = [0, 1, 5, 213, 2906, 200_000, 250_000]
WRITTEN = [_ABSENT, None, 0, 5, "0", "12.5", "-0.25", "1000000.000000000000000000000001"]


class _Raw:
    """A value stored as it is (a string stays a string)."""

    def __init__(self, value):
        self.value = value


def _value(value):
    if isinstance(value, _Raw):
        return value.value
    return ContractingDecimal(value) if isinstance(value, str) else value


def case_list(seed=SEED, count=CASES, balances=BALANCES, written=WRITTEN):
    rng = random.Random(seed)
    return [
        {
            "balance": _value(rng.choice(balances)),
            "budget": rng.choice(BUDGETS),
            "rate": rng.choice(RATES),
            "chi_used": rng.choice(CHI_USED),
            "written": _value(rng.choice(written)),
        }
        for _ in range(count)
    ]


def rust_outcome(native, case):
    """The same outcome from the Rust port, or None when it hands back."""
    reserved = native._fee_reserve_debug(case["balance"], case["budget"], case["rate"])
    metered = native._fee_metered_debug(case["balance"], case["chi_used"], case["rate"])
    if reserved is None or metered is None:
        return None
    refunded = None
    if reserved != ("insufficient",):
        written = case["written"]
        refunded = native._fee_refund_debug(
            reserved[0],
            reserved[1],
            case["chi_used"],
            case["rate"],
            None if written is _ABSENT else written,
            written is not _ABSENT,
        )
        if refunded is None:
            return None
    return {
        "reserved": None if reserved == ("insufficient",) else list(reserved),
        "refunded": refunded,
        "metered": metered[1] if metered[0] == "write" else "unchanged",
        "paid": metered[2] if metered[0] == "write" else "0",
    }


def _negative(balance) -> bool:
    return isinstance(balance, int | ContractingDecimal) and balance < 0


def revision_14(case, outcome):
    """A pinned outcome under revision 14: a failure charge never raises a
    negative balance (no write, nothing paid)."""
    if "raises" in outcome or not _negative(case["balance"]):
        return outcome
    return {**outcome, "metered": "unchanged", "paid": "0"}


def _wire(outcome):
    return json.loads(encode(outcome, vm_profile="xian_vm_v2"))


def test_rust_fee_settlement_reproduces_the_pinned_corpus():
    native = pytest.importorskip("xian_vm_core._native")
    expected = json.loads(FIXTURE.read_text())
    for index, case in enumerate(case_list()):
        outcome = rust_outcome(native, case)
        if "raises" in expected[index]:
            assert outcome is None, index
            continue
        assert outcome is not None, index
        assert _wire(outcome) == revision_14(case, expected[index]), index


def wide_cases():
    odd = [_Raw(value) for value in (True, "5", "", [1], {"a": 1})]
    return case_list(seed=SEED + 1, count=1500, balances=BALANCES + odd, written=WRITTEN + odd)


def test_rust_fee_settlement_reproduces_the_frozen_wide_corpus():
    native = pytest.importorskip("xian_vm_core._native")
    frozen = json.loads(WIDE_FIXTURE.read_text())
    answered = 0
    for index, case in enumerate(wide_cases()):
        outcome = rust_outcome(native, case)
        assert (None if outcome is None else _wire(outcome)) == frozen[index], index
        answered += outcome is not None
    assert answered > 700
