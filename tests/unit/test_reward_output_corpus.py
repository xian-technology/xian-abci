"""Pin the transaction reward outputs over many reward configurations.

Fee and reward accounting runs in Rust (vm-core ``tx_rewards``); it must
reproduce these outputs exactly (Python decimal at 28 digits,
ContractingDecimal conversion and division by the chi rate). The cases are
generated from a fixed seed; ``tests/fixtures/reward_outputs.json`` holds
what the Python reference (deleted in increment 9 of the single-engine plan)
computed for them, encoded as wire JSON.

``tests/fixtures/failed_reward_outputs.json`` pins the payout of a failed
transaction over the same configurations: no developer reward, and a paid
chi that is a ``ContractingDecimal`` when the sender paid its whole balance.

The fixtures are not regenerated.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

native = pytest.importorskip("xian_vm_core._native")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reward_outputs.json"
FAILED_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "failed_reward_outputs.json"
SEED = 20261113
CASES = 160

SPLITS = [
    ["0.5", "0.2", "0.2", "0.1"],
    ["0.25", "0.25", "0.25", "0.25"],
    [
        "0.333333333333333333333333333333",
        "0",
        "0.333333333333333333333333333333",
        "0.333333333333333333333333333334",
    ],
    ["1", "0", "0", "0"],
    ["0", "0", "0", "1"],
    ["0.07", "0.9", "0.01", "0.02"],
]
NUMBERS = [
    None,
    0,
    -3,
    1,
    7,
    1000,
    "0.5",
    "3.14159265358979323846264338327",
    "1000000.000000000000000000000001",
]


def _number(rng):
    value = rng.choice(NUMBERS)
    return ContractingDecimal(value) if isinstance(value, str) else value


def _case(rng, index):
    state = {
        "rewards.S:value": [ContractingDecimal(part) for part in rng.choice(SPLITS)],
        "chi_cost.S:value": rng.choice([1, 3, 7, 20, 2000]),
        "foundation.owner": rng.choice(["foundation", "f" * 64]),
    }
    validators = [f"val{index}_{n}" for n in range(rng.randrange(5))]
    state["validators.active_validators"] = validators
    for validator in validators:
        state[f"validators.powers:{validator}"] = _number(rng)
        state[f"validators.reward_keys:{validator}"] = rng.choice([None, "", f"rk_{validator}"])
        state[f"validators.commission_bps:{validator}"] = rng.choice(
            [None, 0, 500, 10000, 12000, -5]
        )
        state[f"validators.self_bond:{validator}"] = _number(rng)
        delegators = [f"del{n}" for n in range(rng.randrange(4))]
        state[f"validators.delegator_lists:{validator}"] = delegators
        for delegator in delegators:
            state[f"validators.delegations:{delegator}:{validator}"] = _number(rng)
            state[f"validators.delegator_reward_keys:{delegator}:{validator}"] = rng.choice(
                [None, "", f"drk_{delegator}"]
            )
    contracts = [f"con_{n}" for n in range(rng.randrange(1, 4))]
    for contract in contracts:
        state[f"{contract}.__developer__"] = rng.choice([None, "sys", "", f"dev_{contract}"])
    contract_costs = rng.choice(
        [None, {}, {contract: rng.choice([0, 1, 250, 12345, -2]) for contract in contracts}]
    )
    return {
        "state": state,
        "total_chi_to_split": rng.choice([1, 213, 2906, 99_999, 1_000_007]),
        "contract": contracts[0],
        "contract_costs": contract_costs,
    }


def cases():
    rng = random.Random(SEED)
    return [_case(rng, index) for index in range(CASES)]


# What a failed transaction paid: chi used, or a whole balance priced in chi.
FAILED_TOTALS = [
    0,
    5,
    213,
    99_999,
    "0.006",
    "2.46913578024691357802469135782",
    "0.000000000000000000000000000003",
    "20000000.000000000000000000000020",
]


def failed_cases():
    rng = random.Random(SEED + 1)
    return [
        {**case, "total_chi_to_split": _total(rng.choice(FAILED_TOTALS)), "pay_developers": False}
        for case in cases()
    ]


def _total(value):
    return ContractingDecimal(value) if isinstance(value, str) else value


def outputs(case):
    rewards, deltas, records = native._tx_rewards_debug(
        case["state"],
        case["total_chi_to_split"],
        case["contract"],
        case["contract_costs"],
        pay_developers=case.get("pay_developers", True),
    )
    return json.loads(encode([rewards, deltas, records], vm_profile="xian_vm_v2"))


def test_reward_outputs_match_the_pinned_corpus():
    expected = json.loads(FIXTURE.read_text())
    assert len(expected) == CASES
    for index, case in enumerate(cases()):
        assert outputs(case) == expected[index], index


def test_failed_transaction_reward_outputs_match_the_pinned_corpus():
    expected = json.loads(FAILED_FIXTURE.read_text())
    assert len(expected) == CASES
    for index, case in enumerate(failed_cases()):
        assert outputs(case) == expected[index], index


def test_a_failed_transaction_pays_its_developer_share_to_the_validators():
    developer_paid = 0
    for case in failed_cases():
        rewards, _deltas, records = outputs(case)
        assert not (rewards or {}).get("developer_reward"), case
        assert all(record["type"] != "developer_reward" for record in records)
        succeeded = outputs({**case, "pay_developers": True})
        developer_paid += bool((succeeded[0] or {}).get("developer_reward"))
    # The corpus has configurations where a success would pay developers.
    assert developer_paid > 20
