"""The Rust reward port (xian_vm_core.tx_rewards) against the Python reference.

The pinned corpus must come out byte-identical. A wider generated corpus adds
value types and magnitudes the node should never see. There execution
revision 14's rule applies: a reward input of a type it may not hold (or an
amount the decimal context cannot represent) means no rewards for that
transaction. Whenever the port pays rewards, Python must pay the same; when
it pays none, Python pays none too, raises, or the case has such an input.
"""

from __future__ import annotations

import random

import pytest
from test_reward_output_corpus import FAILED_FIXTURE, FIXTURE, _Client, cases, failed_cases
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode

from xian.rewards import RewardsHandler

native = pytest.importorskip("xian_vm_core._native")


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


def _python(case):
    handler = RewardsHandler(_Client(case["state"]))
    return handler.build_tx_reward_outputs(
        total_chi_to_split=case["total_chi_to_split"],
        contract=case["contract"],
        contract_costs=case["contract_costs"],
        pay_developers=case.get("pay_developers", True),
    )


@pytest.mark.parametrize("fixture, corpus", [(FIXTURE, cases), (FAILED_FIXTURE, failed_cases)])
def test_rust_rewards_reproduce_the_pinned_corpus(fixture, corpus):
    import json

    expected = json.loads(fixture.read_text())
    for index, case in enumerate(corpus()):
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
        "total_chi_to_split": rng.choice(
            [0, 1, 213, 99_999, 10**7, -5, 10**50]
            + [ContractingDecimal(text) for text in ("0.006", "-0.5", "3E-30", "9" * 60)]
        ),
        "contract": contracts[0],
        "contract_costs": costs,
        "pay_developers": rng.random() < 0.5,
    }


# Shares are computed at the 28-digit decimal context and rounded to 8
# places: inputs this large overflow it (Python turns that into a zero
# foundation reward, revision 14 into no rewards).
REPRESENTABLE = 10**20


def _number(value) -> bool:
    return value is None or (
        isinstance(value, int | ContractingDecimal)
        and not isinstance(value, bool)
        and abs(value) < REPRESENTABLE
    )


def _text(value) -> bool:
    return value is None or isinstance(value, str)


def _names(value) -> bool:
    return not value or (isinstance(value, list) and all(isinstance(n, str) for n in value))


def _validator_inputs_valid(state, validator) -> bool:
    terms = ("powers", "commission_bps", "self_bond")
    if not all(_number(state.get(f"validators.{term}:{validator}")) for term in terms):
        return False
    if not _text(state.get(f"validators.reward_keys:{validator}")):
        return False
    delegators = state.get(f"validators.delegator_lists:{validator}")
    if not _names(delegators):
        return False
    return all(
        _number(state.get(f"validators.delegations:{delegator}:{validator}"))
        and _text(state.get(f"validators.delegator_reward_keys:{delegator}:{validator}"))
        for delegator in delegators or []
    )


def _reward_inputs_valid(case) -> bool:
    """Revision 14's reward input types (the Rust rule, restated)."""
    state = case["state"]
    validators = state.get("validators.active_validators")
    return (
        abs(case["total_chi_to_split"]) < REPRESENTABLE
        and _split_valid(state.get("rewards.S:value"))
        and _rate_valid(state.get("chi_cost.S:value"))
        and _text(state.get("foundation.owner"))
        and _names(validators)
        and all(_validator_inputs_valid(state, v) for v in validators or [])
        and _developers_valid(case)
    )


def _rate_valid(rate) -> bool:
    return type(rate) is int and rate > 0


def _developers_valid(case) -> bool:
    state = case["state"]
    contracts = [*(case["contract_costs"] or {}), case["contract"]]
    developers = [state.get(f"{name}.__developer__") for name in contracts]
    return all(not developer or isinstance(developer, str) for developer in developers)


def _split_valid(split) -> bool:
    return (
        isinstance(split, list)
        and len(split) == 4
        and all(_number(part) and part is not None for part in split)
    )


def _python_or_raised(case):
    try:
        return _encoded(_python(case))
    except Exception as exc:  # noqa: BLE001 - Python's own refusal
        return type(exc).__name__


NO_REWARDS = _encoded((None, {}, []))


def test_rust_rewards_pay_what_python_pays_or_nothing_for_invalid_inputs():
    rng = random.Random(20261113)
    paid = unpaid = 0
    for index in range(3000):
        case = _wide_case(rng, index)
        rust = _encoded(_rust(case))
        python = _python_or_raised(case)
        if rust != NO_REWARDS:
            paid += 1
            assert rust == python, index
            continue
        unpaid += 1
        assert python == NO_REWARDS or not _reward_inputs_valid(case) or (
            python in {"InvalidOperation", "TypeError", "ValueError", "KeyError"}
        ), index
    # Both outcomes occur (the pinned corpus covers the paid ones in depth).
    assert paid > 25 and unpaid > 1000


def test_node_rewards_are_computed_in_rust_over_pending_and_committed_state(tmp_path, monkeypatch):
    from contracting.local import ContractingClient

    corpus = cases()[:40] + failed_cases()[:40]
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
                pay_developers=case.get("pay_developers", True),
            )
            assert _encoded(result) == expected[index], index
            driver.apply_writes({key: None for key, _ in items})
            driver.commit()


def _must_not_run(*args, **kwargs):
    raise AssertionError("the Python reward computation ran")
