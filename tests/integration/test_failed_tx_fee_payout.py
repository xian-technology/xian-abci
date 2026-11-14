"""A failed transaction's fee is paid out like a successful one's.

The fee a failed paid transaction is charged goes through the governed reward
split: the validator share to the active validators by power, the foundation
share to ``foundation.owner``, the burn share burned. A failed call earns no
developer reward, so the developer share goes to the validators, as it does
for a contract without a developer. A sender who cannot cover the failure fee
pays its whole balance, and exactly that is paid out. Native supply only
shrinks by the burn share. The per-transaction path and the Rust block path
must agree on all of it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.decimal import ContractingDecimal

from xian.utils.tx import transaction_bytes
from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
RATE = 20
SPLIT = ["0.5", "0.1", "0.2", "0.2"]  # validators, burn, foundation, developers
VALIDATOR_SHARE = Decimal(SPLIT[0]) + Decimal(SPLIT[3])
BURN_SHARE = Decimal(SPLIT[1])
FOUNDATION_SHARE = Decimal(SPLIT[2])
PROBE = """v = Variable()

@export
def ok():
    v.set(1)

@export
def fail():
    v.set(2)
    assert False, 'deliberate failure'
"""
# Stands in for the validators contract: a transaction in the block replaces
# the active set.
VALIDATORS = """active_validators = Variable()
powers = Hash()

@export
def rotate():
    active_validators.set(['val3'])
"""
BALANCES = {
    "alice": ContractingDecimal("1000"),
    "dave": ContractingDecimal("0.0003"),
    # Exactly the 5-chi base fee at 20 chi per token.
    "frank": ContractingDecimal("0.25"),
}


@pytest.fixture(params=["python", "rust"])
def chain(request, tmp_path, monkeypatch):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=tmp_path / "state")
    driver = client.raw_driver
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_probe", PROBE, developer="dev")
    driver.set_contract_from_source("validators", VALIDATORS)
    for account, balance in BALANCES.items():
        driver.set(f"currency.balances:{account}", balance)
    driver.set("chi_cost.S:value", RATE)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["val1", "val2"])
    driver.set("validators.powers:val2", 3)
    driver.set("rewards.S:value", [ContractingDecimal(part) for part in SPLIT])
    driver.commit()
    processor = TxProcessor(client=client)
    rewards = RewardsHandler(client)
    python_runs = []
    process_tx = processor.process_tx

    def counted(tx, *args, **kwargs):
        python_runs.append(tx["payload"]["function"])
        return process_tx(tx, *args, **kwargs)

    monkeypatch.setattr(processor, "process_tx", counted)

    def run(txs, policy=None):
        policy = policy or TxFeePolicy.paid_metered()
        if request.param == "rust":
            responses = processor.process_block(
                txs, [transaction_bytes(tx) for tx in txs], fee_policy=policy, rewards_handler=rewards
            )
        else:
            responses = [processor.process_tx(tx, rewards, fee_policy=policy) for tx in txs]
        driver.commit()
        return [response["tx_result"] for response in responses]

    yield run, driver, request.param, python_runs
    driver.close()


def _tx(sender, function, *, contract="con_probe", kwargs=None, budget=1000):
    return {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": function,
            "kwargs": kwargs or {},
            "nonce": 0,
            "chi_supplied": budget,
        },
        "metadata": {"signature": f"{sender}-{contract}-{function}"},
        "b_meta": {"nanos": 1_000_000_000, "height": 1, "hash": "ab", "chain_id": "local"},
    }


def _balance(driver, account):
    return Decimal(str(driver.get(f"currency.balances:{account}") or 0))


def _supply(driver):
    return sum(Decimal(str(value)) for value in driver.values("currency.balances:"))


def _assert_paid_out(driver, fee, validators):
    """``fee`` tokens went to the validators (by power) and the foundation."""
    validator_total = fee * VALIDATOR_SHARE
    total_power = sum(validators.values())
    for validator, power in validators.items():
        assert _balance(driver, validator) == validator_total * power / total_power, validator
    assert _balance(driver, "foundation") == fee * FOUNDATION_SHARE
    assert _balance(driver, "dev") == 0


def test_failed_transaction_fee_goes_to_validators_and_foundation(chain):
    run, driver, path, python_runs = chain
    supply = _supply(driver)

    [result] = run([_tx("alice", "fail")])

    assert result["status"] == 1
    fee = Decimal(result["chi_used"]) / RATE
    assert _balance(driver, "alice") == 1000 - fee
    _assert_paid_out(driver, fee, {"val1": 1, "val2": 3})
    assert result["rewards"]["developer_reward"] == {}
    assert {record["type"] for record in result["reward_records"]} == {
        "validator_reward",
        "foundation_reward",
    }
    assert _supply(driver) == supply - fee * BURN_SHARE
    assert driver.get("con_probe.v") is None
    if path == "rust":
        assert python_runs == []


def test_a_successful_transaction_still_pays_its_developer(chain):
    run, driver, _path, _runs = chain
    supply = _supply(driver)

    [result] = run([_tx("alice", "ok")])

    assert result["status"] == 0
    fee = Decimal(result["chi_used"]) / RATE
    assert _balance(driver, "dev") == fee * Decimal(SPLIT[3])
    assert _balance(driver, "val1") + _balance(driver, "val2") == fee * Decimal(SPLIT[0])
    assert _supply(driver) == supply - fee * BURN_SHARE


def test_a_sender_who_cannot_cover_the_failure_fee_pays_out_its_whole_balance(chain):
    run, driver, _path, _runs = chain
    supply = _supply(driver)

    # dave cannot reserve the budget; the 5-chi base fee (0.25) exceeds his
    # 0.0003, so he pays 0.0003.
    [result] = run([_tx("dave", "fail")])

    assert result["status"] == 1
    assert result["chi_used"] == 5
    assert _balance(driver, "dave") == 0
    paid = Decimal("0.0003")
    _assert_paid_out(driver, paid, {"val1": 1, "val2": 3})
    assert _supply(driver) == supply - paid * BURN_SHARE


def test_a_balance_of_exactly_the_failure_fee_is_paid_out_whole(chain):
    run, driver, _path, _runs = chain

    # frank cannot reserve the budget; the base fee takes his whole balance.
    [result] = run([_tx("frank", "fail")])

    assert result["status"] == 1
    assert result["chi_used"] == 5
    assert _balance(driver, "frank") == 0
    fee = Decimal("0.25")
    _assert_paid_out(driver, fee, {"val1": 1, "val2": 3})


def test_a_validator_is_charged_and_paid_by_its_own_failure(chain):
    run, driver, _path, _runs = chain
    driver.set("currency.balances:val1", ContractingDecimal("10"))
    driver.commit()
    supply = _supply(driver)

    [result] = run([_tx("val1", "fail", budget=100)])

    assert result["status"] == 1
    fee = Decimal(result["chi_used"]) / RATE
    assert _balance(driver, "val1") == 10 - fee + fee * VALIDATOR_SHARE / 4
    assert _supply(driver) == supply - fee * BURN_SHARE


def test_a_sender_without_balance_pays_out_nothing(chain):
    run, driver, _path, _runs = chain
    supply = _supply(driver)

    [result] = run([_tx("carol", "fail")])

    assert result["status"] == 1
    assert result["rewards"] is None
    assert result["reward_records"] is None
    assert _balance(driver, "val1") == _balance(driver, "val2") == 0
    assert _supply(driver) == supply


def test_a_failure_after_the_validator_set_changed_pays_the_new_set(chain):
    run, driver, _path, _runs = chain

    rotate, failed = run([_tx("alice", "rotate", contract="validators"), _tx("alice", "fail")])

    assert rotate["status"] == 0 and failed["status"] == 1
    rotate_fee = Decimal(rotate["chi_used"]) / RATE
    fee = Decimal(failed["chi_used"]) / RATE
    # rotate has no developer, so its whole validator share went to the old set.
    assert _balance(driver, "val1") + _balance(driver, "val2") == rotate_fee * VALIDATOR_SHARE
    assert _balance(driver, "val3") == fee * VALIDATOR_SHARE


def test_supply_shrinks_only_by_the_burn_share_in_a_mixed_block(chain):
    run, driver, _path, _runs = chain
    supply = _supply(driver)
    transfer = _tx(
        "alice",
        "transfer",
        contract="currency",
        kwargs={"amount": {"__fixed__": "1"}, "to": "erin"},
    )

    results = run(
        [
            _tx("alice", "ok"),
            _tx("alice", "fail"),
            _tx("dave", "fail"),
            _tx("carol", "fail"),
            transfer,
            _tx("dave", "fail"),
            _tx("alice", "fail", budget=3),
        ]
    )

    assert [result["status"] for result in results] == [0, 1, 1, 1, 0, 1, 1]
    alice_fees = sum(Decimal(results[index]["chi_used"]) / RATE for index in (0, 1, 4, 6))
    paid = alice_fees + Decimal("0.0003")
    assert _supply(driver) == supply - paid * BURN_SHARE
    assert _balance(driver, "erin") == 1


@pytest.mark.parametrize(
    "policy",
    [
        TxFeePolicy.free_metered(max_tx_chi=1000, max_block_chi=1000),
        TxFeePolicy.system_unmetered(),
    ],
)
def test_modes_without_fee_rewards_pay_nothing_for_a_failure(chain, policy):
    run, driver, _path, _runs = chain
    supply = _supply(driver)

    [result] = run([_tx("alice", "fail")], policy)

    assert result["status"] == 1
    assert result["rewards"] is None
    assert _balance(driver, "alice") == 1000
    assert _supply(driver) == supply
