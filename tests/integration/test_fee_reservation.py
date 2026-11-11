"""Paid execution isolates the submitted fee budget from contract spending."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from contracting.storage.driver import Driver
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode_wire_numbers

from xian.execution_engine import VmRuntime, execute_vm_transaction
from xian.fee_policy import TxFeePolicy
from xian.parallel_executor import ParallelBlockExecutor
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.simulator import TransactionSimulator

ROOT = Path(__file__).resolve().parents[3]
PROBE = """import currency
value = Variable()
@export
def record():
    value.set(123)
    return currency.balance_of(address=ctx.caller)
@export
def spend(amount: decimal, fail: bool = False):
    value.set(456)
    currency.transfer_from(amount=amount, to='recipient', main_account=ctx.caller)
    assert not fail, 'deliberate failure'
"""
RATE = """S = Hash()
@export
def set_value(new_value: int):
    S['value'] = new_value
"""


def make_processor(home, *, balance=100, rate=100):
    driver = Driver(vm_profile="xian_vm_v2", storage_home=home)
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_probe", PROBE)
    driver.set_contract_from_source("chi_cost", RATE)
    for account in ("alice", "bob"):
        driver.set(f"currency.balances:{account}", balance)
        driver.set(f"currency.approvals:{account}:con_probe", 100)
    driver.set("chi_cost.S:value", rate)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["validator"])
    driver.set("rewards.S:value", [1, 0, 0, 0])
    driver.commit()
    client = SimpleNamespace(raw_driver=driver, get_var=driver.get_var)
    return TxProcessor(client=client), RewardsHandler(client)


def tx(*, sender="alice", contract="con_probe", function="record", kwargs=None, budget=1000):
    return {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": function,
            "kwargs": kwargs or {},
            "nonce": 0,
            "chi_supplied": budget,
        },
        "metadata": {"signature": sender + function},
        "b_meta": {"nanos": 1_000_000_000, "height": 1, "hash": "ab", "chain_id": "local"},
    }


def process(processor, rewards, transaction, *, policy=None):
    return processor.process_tx(
        transaction,
        rewards_handler=rewards,
        fee_policy=policy or TxFeePolicy.paid_metered(),
        track_access=True,
    )


@pytest.mark.parametrize("balance", [0, 9])
def test_unfunded_budget_has_no_execution_effects_fee_or_rewards(tmp_path, balance):
    processor, rewards = make_processor(tmp_path, balance=balance)
    driver = processor.client.raw_driver
    try:
        response = process(processor, rewards, tx())
        result = response["tx_result"]
        assert result is not None
        assert result["status"] == 1
        assert "chi" in result["result"].lower()
        # A failed transaction pays base chi from whatever balance remains.
        charged = max(balance - ContractingDecimal(TRANSACTION_BASE_CHI) / 100, 0)
        assert result["chi_used"] == TRANSACTION_BASE_CHI
        assert result["state"] == [{"key": "currency.balances:alice", "value": charged}]
        assert result["events"] == []
        assert result["rewards"] is None
        assert response["reward_deltas"] == {}
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == charged
        assert "currency.balances:alice" in response["access"].reads
        assert "chi_cost.S:value" in response["access"].reads
    finally:
        driver.close()


@pytest.mark.parametrize("balance", [10, 100])
def test_paid_execution_sees_spendable_balance_and_refunds_unused_budget(tmp_path, balance):
    processor, rewards = make_processor(tmp_path, balance=balance)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx())["tx_result"]
        assert result["status"] == 0, result
        # 1000 chi at 100 chi/token reserves ten native tokens during execution.
        assert decode_wire_numbers(result["result"]) == balance - 10
        fee = ContractingDecimal(result["chi_used"]) / 100
        assert 0 < fee < 10
        assert driver.get("currency.balances:alice") == balance - fee
        assert driver.get("con_probe.value") == 123
        assert driver.get("currency.balances:validator") == fee
    finally:
        driver.close()


def test_paid_exhaustion_charges_reserved_budget_and_discards_contract_effects(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx(budget=1))["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == 1
        assert driver.get("currency.balances:alice") == ContractingDecimal("99.99")
        assert driver.get("con_probe.value") is None
        assert result["events"] == []
        assert result["rewards"] is None
    finally:
        driver.close()


def test_unfunded_cost_estimation_runs_without_reserving_inferred_budget(tmp_path):
    processor, _ = make_processor(tmp_path, balance=0)
    driver = processor.client.raw_driver
    try:
        before = driver.snapshot_state()
        estimate = TransactionSimulator(
            client=processor.client, execution_runtime=VmRuntime(), chain_id="local"
        ).simulate(
            {
                "sender": "alice",
                "contract": "con_probe",
                "function": "record",
                "kwargs": {},
            }
        )
        assert estimate["status"] == 0, estimate
        assert estimate["chi_used"] > 0
        assert driver.snapshot_state() == before
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == 0
    finally:
        driver.close()


def test_nonterminating_conversion_refund_preserves_exact_charged_fee(tmp_path):
    processor, rewards = make_processor(tmp_path, balance=1000, rate=3)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx())["tx_result"]
        assert result["status"] == 0, result
        fee = ContractingDecimal(result["chi_used"]) / 3
        assert driver.get("currency.balances:alice") == 1000 - fee
        assert driver.get("currency.balances:validator") == fee
    finally:
        driver.close()


def test_affordability_check_does_not_round_required_budget_down(tmp_path):
    balance = ContractingDecimal("0.333333333333333333333333333333")
    processor, rewards = make_processor(tmp_path, balance=balance, rate=3)
    driver = processor.client.raw_driver
    try:
        # One chi requires strictly more than this truncated token amount.
        result = process(processor, rewards, tx(budget=1))["tx_result"]
        assert result["status"] == 1
        assert "enough chi" in result["result"]
        assert driver.get("con_probe.value") is None
        # The failure charge is capped at the one submitted chi.
        assert result["chi_used"] == 1
        assert driver.get("currency.balances:alice") == max(balance - ContractingDecimal(1) / 3, 0)
    finally:
        driver.close()


def test_direct_paid_execution_rejects_zero_budget_without_running_contract(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx(budget=0))["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == 0
        assert result["state"] == []
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


@pytest.mark.parametrize("charge_failure", [False, True])
def test_direct_helper_preserves_its_explicit_failure_settlement_policy(tmp_path, charge_failure):
    processor, _ = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        outcome = execute_vm_transaction(
            VmRuntime(),
            driver,
            sender="alice",
            contract_name="con_probe",
            function_name="spend",
            kwargs={"amount": 50, "fail": True},
            environment={},
            chi_budget=1000,
            chi_cost=100,
            meter=True,
            apply_metering_on_success_only=not charge_failure,
        )
        assert outcome.output.status_code == 1
        assert outcome.chi_used > 0
        expected = {"currency.balances:alice": 100 - ContractingDecimal(outcome.chi_used) / 100}
        assert outcome.writes == (expected if charge_failure else {})
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


@pytest.mark.parametrize("amount, deliberate_failure", [(91, False), (50, True)])
def test_contract_failure_rolls_back_spending_and_charges_only_consumed_chi(
    tmp_path, amount, deliberate_failure
):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(
            processor,
            rewards,
            tx(function="spend", kwargs={"amount": amount, "fail": deliberate_failure}),
        )["tx_result"]
        assert result["status"] == 1
        fee = ContractingDecimal(result["chi_used"]) / 100
        assert 0 < fee <= 10
        assert driver.get("currency.balances:alice") == 100 - fee
        assert driver.get("currency.balances:recipient") is None
        assert driver.get("con_probe.value") is None
        assert result["events"] == []
        assert result["rewards"] is None
        assert {write["key"] for write in result["state"]} == {"currency.balances:alice"}
    finally:
        driver.close()


def test_successful_native_spend_cannot_consume_reserved_fee(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx(function="spend", kwargs={"amount": 90}))[
            "tx_result"
        ]
        assert result["status"] == 0, result
        fee = ContractingDecimal(result["chi_used"]) / 100
        assert driver.get("currency.balances:alice") == 10 - fee
        assert driver.get("currency.balances:recipient") == 90
        assert driver.get("currency.balances:validator") == fee
    finally:
        driver.close()


def test_free_metered_execution_preserves_full_spendable_balance(tmp_path):
    processor, rewards = make_processor(tmp_path, balance=1)
    driver = processor.client.raw_driver
    try:
        result = process(
            processor,
            rewards,
            tx(function="spend", kwargs={"amount": 1}),
            policy=TxFeePolicy.free_metered(),
        )["tx_result"]
        assert result["status"] == 0, result
        assert result["chi_used"] > 0
        assert driver.get("currency.balances:alice") == 0
        assert driver.get("currency.balances:recipient") == 1
        assert result["rewards"] is None
    finally:
        driver.close()


def test_free_metered_execution_does_not_debit_untouched_sender_balance(tmp_path):
    processor, rewards = make_processor(tmp_path, balance=1)
    driver = processor.client.raw_driver
    try:
        result = process(processor, rewards, tx(), policy=TxFeePolicy.free_metered())["tx_result"]
        assert result["status"] == 0, result
        assert result["chi_used"] > 0
        assert driver.get("currency.balances:alice") == 1
        assert all(write["key"] != "currency.balances:alice" for write in result["state"])
        assert result["rewards"] is None
    finally:
        driver.close()


def test_cached_chi_rate_update_changes_next_reservation(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        first = process(
            processor,
            rewards,
            tx(contract="chi_cost", function="set_value", kwargs={"new_value": 200}),
        )
        assert first["tx_result"]["status"] == 0
        second = process(processor, rewards, tx(sender="bob"))
        assert second["tx_result"]["status"] == 0
        assert decode_wire_numbers(second["tx_result"]["result"]) == 95
        fee = ContractingDecimal(second["tx_result"]["chi_used"]) / 200
        assert driver.get("currency.balances:bob") == 100 - fee
        assert "chi_cost.S:value" in second["access"].reads
    finally:
        driver.close()


@pytest.mark.parametrize("receives_funds", [True, False])
@pytest.mark.parametrize("workers", [1, 2])
def test_paid_parallel_execution_matches_serial_after_funding_change(
    tmp_path, receives_funds, workers
):
    balance = 9 if receives_funds else 11
    serial, serial_rewards = make_processor(tmp_path / "serial", balance=balance)
    parallel, parallel_rewards = make_processor(tmp_path / "parallel", balance=balance)
    for processor in (serial, parallel):
        processor.client.raw_driver.set("currency.balances:alice", 100)
        processor.client.raw_driver.set("currency.approvals:bob:alice", 100)
        processor.client.raw_driver.commit()
    transactions = [
        tx(
            contract="currency",
            function="transfer" if receives_funds else "transfer_from",
            kwargs={"amount": 2, "to": "bob"}
            if receives_funds
            else {
                "amount": 2,
                "to": "alice",
                "main_account": "bob",
            },
        ),
        tx(sender="bob"),
    ]
    executor = ParallelBlockExecutor(
        storage_home=tmp_path / "parallel",
        enabled=True,
        workers=workers,
        min_transactions=1,
        access_estimates_enabled=False,
    )
    try:
        expected = [
            process(serial, serial_rewards, transaction) for transaction in deepcopy(transactions)
        ]
        actual, stats = executor.execute(
            txs=deepcopy(transactions),
            tx_processor=parallel,
            fee_policy=TxFeePolicy.paid_metered(),
            rewards_handler=parallel_rewards,
        )
        assert [response["tx_result"] for response in actual] == [
            response["tx_result"] for response in expected
        ]
        assert [response["tx_result"]["status"] for response in actual] == [
            0,
            0 if receives_funds else 1,
        ]
        assert stats.speculative_rejected >= 1
        for key in (
            "currency.balances:alice",
            "currency.balances:bob",
            "currency.balances:validator",
            "con_probe.value",
        ):
            assert parallel.client.raw_driver.get(key) == serial.client.raw_driver.get(key)
    finally:
        executor.close()
        serial.client.raw_driver.close()
        parallel.client.raw_driver.close()


def test_fee_reservation_is_restored_when_vm_boundary_raises(tmp_path):
    processor, _ = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        before = driver.snapshot_state()

        def fail_after_host_write(*_args, **_kwargs):
            driver.apply_writes({"con_probe.value": "partial host write"})
            raise RuntimeError("host failure")

        with patch("xian.execution_engine.execute_vm_contract", side_effect=fail_after_host_write):
            with pytest.raises(RuntimeError, match="host failure"):
                execute_vm_transaction(
                    VmRuntime(),
                    driver,
                    sender="alice",
                    contract_name="con_probe",
                    function_name="record",
                    kwargs={},
                    environment={},
                    chi_budget=1000,
                    chi_cost=100,
                    meter=True,
                )
        assert driver.snapshot_state() == before
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


def test_direct_helper_checks_exact_affordability_with_fractional_rate(tmp_path):
    # The generic accounting helper accepts exact rates independently of the
    # canonical governance contract's stricter integer ABI.
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set("currency.balances:alice", ContractingDecimal("0.5"))
        with patch("xian.execution_engine.execute_vm_contract") as execute:
            outcome = execute_vm_transaction(
                VmRuntime(),
                driver,
                sender="alice",
                contract_name="con_probe",
                function_name="record",
                kwargs={},
                environment={},
                chi_budget=1,
                chi_cost=ContractingDecimal("1.5"),
                meter=True,
            )
        assert outcome.output.status_code == 1
        assert "not have enough chi" in str(outcome.output.result)
        assert outcome.chi_used == 1
        assert outcome.writes == {}
        assert driver.get("currency.balances:alice") == ContractingDecimal("0.5")
        execute.assert_not_called()


def test_canonical_chi_rate_export_rejects_fractional_rate_before_writing_state(tmp_path):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set_contract_from_source(
            "chi_cost", (ROOT / "xian-configs/contracts/chi_cost.s.py").read_text()
        )
        driver.set("chi_cost.S:value", 100)
        driver.commit()
        outcome = execute_vm_transaction(
            VmRuntime(),
            driver,
            sender="validators",
            contract_name="chi_cost",
            function_name="set_value",
            kwargs={"new_value": ContractingDecimal("1.5")},
            environment={},
            chi_budget=0,
            chi_cost=100,
            meter=False,
        )
        assert outcome.output.status_code == 1
        assert "int" in str(outcome.output.result)
        assert outcome.writes == {}
        assert driver.get("chi_cost.S:value") == 100
