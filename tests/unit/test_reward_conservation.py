"""Fee rewards conserve value: paid out plus burned is the charged fee.

A transaction is charged ``chi / rate`` (a ``ContractingDecimal`` truncated
to 30 places). Its rewards are paid from that fee: every share is truncated,
none is negative, and their sum never exceeds the fee. What is not paid
(the burn share and the truncation dust) is burned. A validator's
delegators are paid through its pool: the pool's credit goes to the
``validators`` balance, and the rate per share it adds can never let the
pool's shares claim more than that credit. A split whose paid
ratios are negative or sum to more than 1 pays no rewards (the fee is
burned).
"""

from __future__ import annotations

import random

import pytest
from test_reward_output_corpus import _case, cases, failed_cases
from xian_runtime_types.decimal import ContractingDecimal

native = pytest.importorskip("xian_vm_core._native")


def _paid(case):
    rewards, deltas, _records = native._tx_rewards_debug(
        case["state"],
        case["total_chi_to_split"],
        case["contract"],
        case["contract_costs"],
        pay_developers=case.get("pay_developers", True),
    )
    return rewards, deltas


def _fee(case):
    total = case["total_chi_to_split"]
    return ContractingDecimal(total) / ContractingDecimal(case["state"]["chi_cost.S:value"])


def _assert_conserves(case):
    rewards, deltas = _paid(case)
    if rewards is None:
        return False
    assert all(amount >= 0 for amount in deltas.values()), deltas
    balances = {key: amount for key, amount in deltas.items() if key.startswith("currency.")}
    paid = sum(balances.values(), ContractingDecimal(0))
    assert paid <= _fee(case), (paid, _fee(case), case["state"]["rewards.S:value"])
    _assert_pools_hold_their_claims(case, deltas)
    return True


def _assert_pools_hold_their_claims(case, deltas):
    """A pool credit reaches the validators balance in full, and its shares
    can never claim more than it: rate per share * shares <= credit."""
    pools = {
        key.removeprefix("validators.reward_pool:"): amount
        for key, amount in deltas.items()
        if key.startswith("validators.reward_pool:")
    }
    credited = deltas.get("currency.balances:validators", ContractingDecimal(0))
    assert sum(pools.values(), ContractingDecimal(0)) == credited, deltas
    for validator, pool in pools.items():
        rate = deltas[f"validators.reward_per_share:{validator}"]
        shares = case["state"][f"validators.total_shares:{validator}"]
        assert rate > 0 and rate * shares <= pool, (validator, rate, shares, pool)


@pytest.mark.parametrize("corpus", [cases, failed_cases], ids=["paid", "failed"])
def test_the_pinned_corpus_never_pays_out_more_than_the_fee(corpus):
    assert sum(_assert_conserves(case) for case in corpus()) > 100


def _ratios(rng):
    parts = [rng.randrange(1_000_000) for _ in range(4)]
    total = sum(parts) + 1
    ratios = [ContractingDecimal(part) / ContractingDecimal(total) for part in parts]
    if rng.random() < 0.3:  # no burn: the other three sum to exactly 1
        ratios[1] = ContractingDecimal(0)
        ratios[3] = ContractingDecimal(1) - ratios[0] - ratios[2]
    return ratios


def test_wide_valid_configurations_never_pay_out_more_than_the_fee():
    rng = random.Random(20261114)
    paid = 0
    for index in range(2000):
        case = _case(rng, index)
        case["state"]["rewards.S:value"] = _ratios(rng)
        case["total_chi_to_split"] = rng.choice(
            [
                1,
                3,
                213,
                2906,
                99_999,
                10**12 + 7,
                ContractingDecimal("2.000000000000000000000000000003"),
            ]
        )
        case["pay_developers"] = rng.random() < 0.5
        paid += _assert_conserves(case)
    assert paid > 1500


@pytest.mark.parametrize(
    "split",
    [
        ["0.6", "0", "0.6", "0"],
        ["-0.1", "0.1", "0.5", "0.5"],
        ["0.5", "0", "0.5", "0.000000000000000000000000000001"],
        ["2", "0", "0", "0"],
    ],
)
def test_a_split_paying_more_than_the_fee_pays_no_rewards(split):
    case = cases()[0]
    case["state"]["rewards.S:value"] = [ContractingDecimal(part) for part in split]
    assert _paid(case) == (None, {})
