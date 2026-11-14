"""The Rust reward computation (vm-core ``tx_rewards``) over a wide corpus.

A generated corpus with value types and magnitudes the node should never
see. There execution revision 14's rule applies: a reward input of a type it
may not hold (or an amount the decimal context cannot represent) means no
rewards for that transaction. Until increment 9 of the single-engine plan
every paid outcome was compared with the Python reference and every unpaid
one checked against it; the outcomes were frozen then (``WIDE_FIXTURE``).
The pinned corpus is ``test_reward_output_corpus``.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

native = pytest.importorskip("xian_vm_core._native")

# The wide corpus's Rust outcomes as 16-hex-digit SHA-256 prefixes of their
# encodings, frozen where they matched the Python reference (before it was
# deleted): "-" for no rewards.
WIDE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reward_outputs_wide.json"


def _encoded(result):
    return encode(list(result), vm_profile="xian_vm_v2")


def _rust(case):
    return native._tx_rewards_debug(
        case["state"],
        case["total_chi_to_split"],
        case["contract"],
        case["contract_costs"],
        pay_developers=case.get("pay_developers", True),
    )


WIDE_NUMBERS = [
    None,
    0,
    -1,
    1,
    3,
    10**40,
    10**61,
    "0",
    "0.000000000000000000000000000001",
    "-0.5",
    "12345678901234567890.123456789012345678901234567890",
    "9" * 60,
]
ODD_VALUES = [True, False, "7", "", [], [1], {"__fixed__": "1"}, {"a": 1}]


def _wide_number(rng):
    if rng.random() < 0.1:
        return rng.choice(ODD_VALUES)
    value = rng.choice(WIDE_NUMBERS)
    return ContractingDecimal(value) if isinstance(value, str) else value


def _wide_case(rng, index):
    split = [_wide_number(rng) for _ in range(4)] if rng.random() < 0.3 else None
    state = {
        "rewards.S:value": split
        or rng.choice(
            [
                [
                    ContractingDecimal("0.5"),
                    0,
                    ContractingDecimal("0.3"),
                    ContractingDecimal("0.2"),
                ],
                [1, 0, 0, 0],
                [0, 0, 0, 1],
                [],
                None,
                [ContractingDecimal("0.4")] * 3,
            ]
        ),
        "chi_cost.S:value": rng.choice(
            [1, 20, 7, 2000, 0, -1, None, True, ContractingDecimal("2")]
        ),
        "foundation.owner": rng.choice(["foundation", "", None, 5]),
    }
    validators = rng.choice(
        [[f"v{index}_{n}" for n in range(rng.randrange(6))], None, "", ["v", 3], "abc"]
    )
    state["validators.active_validators"] = validators
    for validator in validators if isinstance(validators, list) else []:
        validator = str(validator)
        state[f"validators.powers:{validator}"] = _wide_number(rng)
        state[f"validators.reward_keys:{validator}"] = rng.choice([None, "", "rk", 4])
        state[f"validators.commission_bps:{validator}"] = _wide_number(rng)
        state[f"validators.self_bond:{validator}"] = _wide_number(rng)
        delegators = rng.choice([[f"d{n}" for n in range(rng.randrange(5))], None, ["d0", "d0"]])
        state[f"validators.delegator_lists:{validator}"] = delegators
        for delegator in delegators or []:
            state[f"validators.delegations:{delegator}:{validator}"] = _wide_number(rng)
            state[f"validators.delegator_reward_keys:{delegator}:{validator}"] = rng.choice(
                [None, "", "drk", False]
            )
    contracts = [f"c{n}" for n in range(rng.randrange(1, 4))]
    for contract in contracts:
        state[f"{contract}.__developer__"] = rng.choice([None, "sys", "", "dev", 0, 9])
    costs = rng.choice(
        [None, {}, {contract: rng.choice([0, 2, 999, -1, 10**30]) for contract in contracts}]
    )
    return {
        "state": state,
        "total_chi_to_split": rng.choice(
            [0, 1, 213, 99_999, 10**7, -5, 10**50]
            + [ContractingDecimal(text) for text in ("0.006", "-0.5", "3E-30", "9" * 60)]
        ),
        "contract": contracts[0],
        "contract_costs": costs,
        "pay_developers": rng.random() < 0.5,
    }


NO_REWARDS = _encoded((None, {}, []))


def wide_digest(encoded: str) -> str:
    if encoded == NO_REWARDS:
        return "-"
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def wide_cases():
    rng = random.Random(20261113)
    return [_wide_case(rng, index) for index in range(3000)]


def test_rust_rewards_reproduce_the_frozen_wide_corpus():
    frozen = json.loads(WIDE_FIXTURE.read_text())
    paid = unpaid = 0
    for index, case in enumerate(wide_cases()):
        rust = _encoded(_rust(case))
        assert wide_digest(rust) == frozen[index], index
        paid += rust != NO_REWARDS
        unpaid += rust == NO_REWARDS
    # Both outcomes occur (the pinned corpus covers the paid ones in depth).
    assert paid > 25 and unpaid > 1000
