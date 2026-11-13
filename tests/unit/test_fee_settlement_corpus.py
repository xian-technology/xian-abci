"""Pin fee reservation, refunds and failure metering over many inputs.

Step B moves fee settlement into Rust; the port must reproduce these outputs
exactly (exact-ratio reservation check, ContractingDecimal truncation, the
integer 0 that ``max(balance - fee, 0)`` yields for a negative balance).
``tests/fixtures/fee_outputs.json`` holds what the current Python code
computes, encoded as wire JSON. The Rust port must answer every pinned case
identically, and on a wider corpus with odd value types it may only hand a
case back to Python, never answer differently.

Regenerate (only for an intended behaviour change):
    python -m tests.unit.test_fee_settlement_corpus --write
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

from xian.execution_engine import (
    _InsufficientFeeBalance,
    _reserve_vm_fee_budget,
    vm_metering_writes,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fee_outputs.json"
SEED = 20261113
CASES = 400
KEY = "currency.balances:sender"
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


class _Driver:
    def __init__(self, balance):
        self.balance = balance

    def make_key(self, contract, variable, args):
        return f"{contract}.{variable}:{args[0]}"

    def get(self, key):
        assert key == KEY
        return self.balance

    def apply_writes(self, writes):
        pass


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


def python_outcome(case):
    """The reservation, the refunded balance and the failure metering, or
    the name of the exception the Python code raises."""
    try:
        return _python_outcome(case)
    except Exception as exc:  # noqa: BLE001 - the corpus pins which cases raise
        return {"raises": type(exc).__name__}


def _python_outcome(case):
    driver = _Driver(case["balance"])
    try:
        reservation = _reserve_vm_fee_budget(
            driver,
            sender="sender",
            chi_budget=case["budget"],
            chi_cost=case["rate"],
            currency_contract="currency",
            balances_hash="balances",
        )
    except _InsufficientFeeBalance:
        reserved = refunded = None
    else:
        reserved = [reservation.amount, reservation.spendable_balance]
        written = {} if case["written"] is _ABSENT else {KEY: case["written"]}
        refunded = reservation.refund_writes(
            written, chi_used=case["chi_used"], chi_cost=case["rate"]
        )[KEY]
    metered = vm_metering_writes(
        driver, sender="sender", chi_used=case["chi_used"], chi_cost=case["rate"]
    )
    return {"reserved": reserved, "refunded": refunded, "metered": metered.get(KEY, "unchanged")}


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
    }


def _wire(outcome):
    return json.loads(encode(outcome, vm_profile="xian_vm_v2"))


def test_fee_outcomes_match_the_pinned_corpus():
    expected = json.loads(FIXTURE.read_text())
    assert len(expected) == CASES
    for index, case in enumerate(case_list()):
        assert _wire(python_outcome(case)) == expected[index], index


def test_rust_fee_settlement_reproduces_the_pinned_corpus():
    native = pytest.importorskip("xian_vm_core._native")
    expected = json.loads(FIXTURE.read_text())
    for index, case in enumerate(case_list()):
        outcome = rust_outcome(native, case)
        if "raises" in expected[index]:
            assert outcome is None, index
            continue
        assert outcome is not None, index
        assert encode(outcome, vm_profile="xian_vm_v2") == encode(
            python_outcome(case), vm_profile="xian_vm_v2"
        ), index
        assert _wire(outcome) == expected[index], index


def test_rust_fee_settlement_answers_only_what_python_answers_the_same():
    native = pytest.importorskip("xian_vm_core._native")
    odd = [_Raw(value) for value in (True, "5", "", [1], {"a": 1})]
    cases = case_list(seed=SEED + 1, count=1500, balances=BALANCES + odd, written=WRITTEN + odd)
    answered = 0
    for index, case in enumerate(cases):
        outcome = rust_outcome(native, case)
        if outcome is None:
            continue
        answered += 1
        assert "raises" not in python_outcome(case), index
        assert encode(outcome, vm_profile="xian_vm_v2") == encode(
            python_outcome(case), vm_profile="xian_vm_v2"
        ), index
    assert answered > 700


if __name__ == "__main__" and sys.argv[1:] == ["--write"]:
    lines = (json.dumps(_wire(python_outcome(case)), separators=(",", ":")) for case in case_list())
    FIXTURE.write_text("[\n" + ",\n".join(lines) + "\n]\n")
