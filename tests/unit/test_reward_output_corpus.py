"""Pin the transaction reward outputs over many reward configurations.

Step B moves fee and reward accounting into Rust; the port must reproduce
these outputs exactly (Python decimal at 28 digits, ContractingDecimal
conversion and division by the chi rate). The cases are generated from a
fixed seed; ``tests/fixtures/reward_outputs.json`` holds what the current
Python code computes for them, encoded as wire JSON.

Regenerate (only for an intended behaviour change):
    python -m tests.unit.test_reward_output_corpus --write
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

from xian.rewards import RewardsHandler

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reward_outputs.json"
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


class _Client:
    def __init__(self, state):
        self.state = state

    def get_var(self, contract, variable, arguments=None):
        key = f"{contract}.{variable}"
        if arguments:
            key += ":" + ":".join(str(argument) for argument in arguments)
        return self.state.get(key)


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


def outputs(case):
    handler = RewardsHandler(_Client(case["state"]))
    rewards, deltas, records = handler.build_tx_reward_outputs(
        total_chi_to_split=case["total_chi_to_split"],
        contract=case["contract"],
        contract_costs=case["contract_costs"],
    )
    return json.loads(encode([rewards, deltas, records], vm_profile="xian_vm_v2"))


def test_reward_outputs_match_the_pinned_corpus():
    expected = json.loads(FIXTURE.read_text())
    assert len(expected) == CASES
    for index, case in enumerate(cases()):
        assert outputs(case) == expected[index], index


if __name__ == "__main__" and sys.argv[1:] == ["--write"]:
    lines = (json.dumps(outputs(case), separators=(",", ":")) for case in cases())
    FIXTURE.write_text("[\n" + ",\n".join(lines) + "\n]\n")
