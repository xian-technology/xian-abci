"""The Rust reward port (xian_vm_core.tx_rewards) against the Python reference.

The pinned corpus must come out byte-identical. A wider generated corpus adds
value types and magnitudes the node should never see; there the port may hand
a case back to Python (it returns None), but whenever it answers, Python must
answer the same and must not raise.
"""

from __future__ import annotations

import random

import pytest
from test_reward_output_corpus import FIXTURE, _Client, cases
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

from xian.rewards import RewardsHandler

native = pytest.importorskip("xian_vm_core._native")


def _encoded(result):
    return encode(list(result), vm_profile="xian_vm_v2")


def _rust(case):
    return native._tx_rewards_debug(
        case["state"], case["total_chi_to_split"], case["contract"], case["contract_costs"]
    )


def _python(case):
    handler = RewardsHandler(_Client(case["state"]))
    return handler.build_tx_reward_outputs(
        total_chi_to_split=case["total_chi_to_split"],
        contract=case["contract"],
        contract_costs=case["contract_costs"],
    )


def test_rust_rewards_reproduce_the_pinned_corpus():
    import json

    expected = json.loads(FIXTURE.read_text())
    for index, case in enumerate(cases()):
        result = _rust(case)
        assert result is not None, index
        assert json.loads(_encoded(result)) == expected[index], index
        assert _encoded(result) == _encoded(_python(case)), index


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
        "total_chi_to_split": rng.choice([0, 1, 213, 99_999, 10**7, -5, 10**50]),
        "contract": contracts[0],
        "contract_costs": costs,
    }


def test_rust_rewards_answer_only_what_python_answers_the_same():
    rng = random.Random(20261113)
    answered = 0
    for index in range(3000):
        case = _wide_case(rng, index)
        rust = _rust(case)
        if rust is None:
            continue
        answered += 1
        assert _encoded(rust) == _encoded(_python(case)), index
    # The port must still cover most of this corpus, not hand everything back.
    assert answered > 1000


def test_node_rewards_are_computed_in_rust_over_pending_and_committed_state(tmp_path, monkeypatch):
    from contracting.local import ContractingClient

    corpus = cases()[:40]
    with ContractingClient(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as client:
        driver = client.raw_driver
        handler = RewardsHandler(client)
        expected = [_encoded(_python(case)) for case in corpus]
        # The node never runs the Python computation for these states.
        monkeypatch.setattr(
            RewardsHandler, "calculate_tx_output_rewards", _must_not_run, raising=True
        )
        for index, case in enumerate(corpus):
            items = sorted(case["state"].items())
            half = len(items) // 2
            driver.apply_writes(dict(items[:half]))
            driver.commit()
            driver.apply_writes(dict(items[half:]))
            result = handler.build_tx_reward_outputs(
                total_chi_to_split=case["total_chi_to_split"],
                contract=case["contract"],
                contract_costs=case["contract_costs"],
            )
            assert _encoded(result) == expected[index], index
            driver.apply_writes({key: None for key, _ in items})
            driver.commit()


def _must_not_run(*args, **kwargs):
    raise AssertionError("the Python reward computation ran")
