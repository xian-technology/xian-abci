"""A paid transaction pays half of the chi it supplied but did not use.

Block capacity (``max_block_chi``) is counted over ``chi_supplied``: it is
the only cost known before execution. Before revision 14's unused-chi
charge a sender paid only the chi it used, so three transactions of 166,666
supplied chi filled a 500k block for ~255 chi each and kept every honest
transaction out (rv-network, B-censorship). Now the sender is charged
``used + (supplied - used) // 2``. The extra charge is burned: fee rewards
stay computed on the chi used, so neither a validator nor a contract
developer earns from wasted capacity. A 1-chi margin is free; a client that
supplies its estimate plus the SDKs' 2-chi margin pays one chi more than
its estimate.
"""

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from fee_probe_support import charged_chi, make_processor, process, tx
from xian_runtime_types.decimal import ContractingDecimal

from xian.fee_policy import DEFAULT_MAX_BLOCK_CHI, TxFeePolicy
from xian.utils.tx import transaction_bytes

RATE = 100
PROBE = """value = Variable()
@export
def touch():
    value.set(1)
@export
def fail():
    value.set(1)
    assert False, 'deliberate failure'
"""


@pytest.fixture
def processor(tmp_path):
    processor = make_processor(tmp_path, balance=10_000, rate=RATE)
    driver = processor.client.raw_driver
    driver.set_contract_from_source("con_waste", PROBE)
    driver.commit()
    yield processor
    driver.close()


def supply(driver, accounts=("alice", "bob", "validator")) -> ContractingDecimal:
    return sum(
        (
            ContractingDecimal(driver.get(f"currency.balances:{account}") or 0)
            for account in accounts
        ),
        ContractingDecimal(0),
    )


def used_chi(processor, function: str, budget: int) -> int:
    """The chi the call uses, from a free run (no fee, same envelope size)."""
    driver = processor.client.raw_driver
    result = process(
        processor,
        tx(contract="con_waste", function=function, budget=budget),
        policy=TxFeePolicy.free_metered(),
    )["tx_result"]
    driver.rollback()
    return result["chi_used"]


@pytest.mark.parametrize("function, status", [("touch", 0), ("fail", 1)])
def test_unused_supplied_chi_is_charged_at_half_and_burned(processor, function, status):
    driver = processor.client.raw_driver
    budget = 9_999
    used = used_chi(processor, function, budget)
    before = supply(driver)

    result = process(processor, tx(contract="con_waste", function=function, budget=budget))[
        "tx_result"
    ]

    assert result["status"] == status
    charged = charged_chi(budget, used)
    assert result["chi_used"] == charged
    assert driver.get("currency.balances:alice") == 10_000 - ContractingDecimal(charged) / RATE
    # Rewards are paid on the chi used; the unused-chi charge is burned.
    assert result["rewards"]["validator_reward"] == {"validator": ContractingDecimal(used) / RATE}
    assert before - supply(driver) == ContractingDecimal(charged - used) / RATE


@pytest.mark.parametrize("margin, extra", [(0, 0), (1, 0), (2, 1), (9, 4)])
def test_a_margin_pays_half_of_itself_rounded_down(processor, margin, extra):
    driver = processor.client.raw_driver
    used = used_chi(processor, "touch", 9_000)

    result = process(processor, tx(contract="con_waste", function="touch", budget=used + margin))[
        "tx_result"
    ]

    assert result["status"] == 0
    assert result["chi_used"] == used + extra
    assert driver.get("currency.balances:alice") == 10_000 - ContractingDecimal(used + extra) / RATE


def test_running_out_of_chi_leaves_nothing_unused(processor):
    result = process(processor, tx(contract="con_waste", function="touch", budget=6))["tx_result"]

    assert result["status"] == 1
    assert result["chi_used"] == 6


def test_an_unfunded_budget_pays_the_unused_charge_from_what_is_left(tmp_path):
    processor = make_processor(tmp_path, balance=9, rate=RATE)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx(budget=1000))["tx_result"]

        assert result["status"] == 1
        charged = charged_chi(1000, TRANSACTION_BASE_CHI)
        assert result["chi_used"] == charged
        assert driver.get("currency.balances:alice") == 9 - ContractingDecimal(charged) / RATE
        assert result["rewards"]["validator_reward"] == {
            "validator": ContractingDecimal(TRANSACTION_BASE_CHI) / RATE
        }
    finally:
        driver.close()


def test_free_execution_charges_nothing_for_unused_chi(processor):
    driver = processor.client.raw_driver

    result = process(
        processor,
        tx(contract="con_waste", function="touch", budget=9_999),
        policy=TxFeePolicy.free_metered(),
    )["tx_result"]

    assert result["chi_used"] < 100
    assert driver.get("currency.balances:alice") == 10_000


def test_filling_a_block_by_supplied_chi_costs_half_of_the_block(tmp_path):
    """The B-censorship block: three senders, 166,666 supplied chi each."""
    processor = make_processor(tmp_path, balance=10_000, rate=RATE)
    driver = processor.client.raw_driver
    driver.set_contract_from_source("con_waste", PROBE)
    driver.set("currency.balances:carol", 10_000)
    driver.commit()
    try:
        senders = ("alice", "bob", "carol")
        before = supply(driver, senders)
        txs = [
            tx(sender=sender, contract="con_waste", function="touch", budget=166_666)
            for sender in senders
        ]
        responses = processor.process_block(
            txs,
            [transaction_bytes(transaction) for transaction in txs],
            fee_policy=TxFeePolicy.paid_metered(),
        )
        paid = before - supply(driver, senders)
        charged = sum(response["tx_result"]["chi_used"] for response in responses)
        assert paid == ContractingDecimal(charged) / RATE
        assert charged >= (DEFAULT_MAX_BLOCK_CHI - 3 * 100) // 2
    finally:
        driver.close()
