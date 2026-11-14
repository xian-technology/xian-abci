"""Paid execution isolates the submitted fee budget from contract spending."""

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from contracting.storage.driver import Driver
from fee_probe_support import ROOT, make_processor, process, tx
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import decode_wire_numbers
from xian_vm_core import execute_tx

from xian.execution_engine import vm_call_context
from xian.fee_policy import TxFeePolicy
from xian.simulator import TransactionSimulator


@pytest.mark.parametrize("balance", [0, 9])
def test_unfunded_budget_has_no_execution_effects_and_pays_out_its_fee(tmp_path, balance):
    processor = make_processor(tmp_path, balance=balance)
    driver = processor.client.raw_driver
    try:
        response = process(processor, tx())
        result = response["tx_result"]
        assert result is not None
        assert result["status"] == 1
        assert "chi" in result["result"].lower()
        # A failed transaction pays base chi from whatever balance remains.
        charged = max(balance - ContractingDecimal(TRANSACTION_BASE_CHI) / 100, 0)
        assert result["chi_used"] == TRANSACTION_BASE_CHI
        assert result["events"] == []
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == charged
        if balance == 0:
            # Nothing was paid, so nothing is paid out.
            assert result["state"] == [{"key": "currency.balances:alice", "value": charged}]
            assert result["rewards"] is None
        else:
            # The fee goes to the one validator (split 1/0/0/0).
            fee = balance - charged
            assert result["rewards"]["validator_reward"] == {"validator": fee}
            assert driver.get("currency.balances:validator") == fee
    finally:
        driver.close()


@pytest.mark.parametrize("balance", [10, 100])
def test_paid_execution_sees_spendable_balance_and_refunds_unused_budget(tmp_path, balance):
    processor = make_processor(tmp_path, balance=balance)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx())["tx_result"]
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
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx(budget=1))["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == 1
        assert driver.get("currency.balances:alice") == ContractingDecimal("99.99")
        assert driver.get("con_probe.value") is None
        assert result["events"] == []
        assert result["rewards"]["validator_reward"] == {"validator": ContractingDecimal("0.01")}
        assert driver.get("currency.balances:validator") == ContractingDecimal("0.01")
    finally:
        driver.close()


def test_unfunded_cost_estimation_runs_without_reserving_inferred_budget(tmp_path):
    processor = make_processor(tmp_path, balance=0)
    driver = processor.client.raw_driver
    try:
        before = driver.snapshot_state()
        estimate = TransactionSimulator(
            client=processor.client, chain_id="local"
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
    processor = make_processor(tmp_path, balance=1000, rate=3)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx())["tx_result"]
        assert result["status"] == 0, result
        fee = ContractingDecimal(result["chi_used"]) / 3
        assert driver.get("currency.balances:alice") == 1000 - fee
        assert driver.get("currency.balances:validator") == fee
    finally:
        driver.close()


def test_affordability_check_does_not_round_required_budget_down(tmp_path):
    balance = ContractingDecimal("0.333333333333333333333333333333")
    processor = make_processor(tmp_path, balance=balance, rate=3)
    driver = processor.client.raw_driver
    try:
        # One chi requires strictly more than this truncated token amount.
        result = process(processor, tx(budget=1))["tx_result"]
        assert result["status"] == 1
        assert "enough chi" in result["result"]
        assert driver.get("con_probe.value") is None
        # The failure charge is capped at the one submitted chi.
        assert result["chi_used"] == 1
        assert driver.get("currency.balances:alice") == max(balance - ContractingDecimal(1) / 3, 0)
    finally:
        driver.close()


def test_direct_paid_execution_rejects_zero_budget_without_running_contract(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx(budget=0))["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == 0
        assert result["state"] == []
        assert driver.get("con_probe.value") is None
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


@pytest.mark.parametrize("amount, deliberate_failure", [(91, False), (50, True)])
def test_contract_failure_rolls_back_spending_and_charges_only_consumed_chi(
    tmp_path, amount, deliberate_failure
):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(
            processor,
            tx(function="spend", kwargs={"amount": amount, "fail": deliberate_failure}),
        )["tx_result"]
        assert result["status"] == 1
        fee = ContractingDecimal(result["chi_used"]) / 100
        assert 0 < fee <= 10
        assert driver.get("currency.balances:alice") == 100 - fee
        assert driver.get("currency.balances:recipient") is None
        assert driver.get("con_probe.value") is None
        assert result["events"] == []
        # The charged fee is paid out, to the validator alone.
        assert result["rewards"]["validator_reward"] == {"validator": fee}
        assert driver.get("currency.balances:validator") == fee
        assert {write["key"] for write in result["state"]} == {
            "currency.balances:alice",
            "currency.balances:validator",
        }
    finally:
        driver.close()


def test_successful_native_spend_cannot_consume_reserved_fee(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx(function="spend", kwargs={"amount": 90}))[
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
    processor = make_processor(tmp_path, balance=1)
    driver = processor.client.raw_driver
    try:
        result = process(
            processor,
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
    processor = make_processor(tmp_path, balance=1)
    driver = processor.client.raw_driver
    try:
        result = process(processor, tx(), policy=TxFeePolicy.free_metered())["tx_result"]
        assert result["status"] == 0, result
        assert result["chi_used"] > 0
        assert driver.get("currency.balances:alice") == 1
        assert all(write["key"] != "currency.balances:alice" for write in result["state"])
        assert result["rewards"] is None
    finally:
        driver.close()


def test_cached_chi_rate_update_changes_next_reservation(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        first = process(
            processor,
            tx(contract="chi_cost", function="set_value", kwargs={"new_value": 200}),
        )
        assert first["tx_result"]["status"] == 0
        second = process(processor, tx(sender="bob"))
        assert second["tx_result"]["status"] == 0
        assert decode_wire_numbers(second["tx_result"]["result"]) == 95
        fee = ContractingDecimal(second["tx_result"]["chi_used"]) / 200
        assert driver.get("currency.balances:bob") == 100 - fee
    finally:
        driver.close()


def test_canonical_chi_rate_export_rejects_fractional_rate_before_writing_state(tmp_path):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set_contract_from_source(
            "chi_cost", (ROOT / "xian-configs/contracts/chi_cost.s.py").read_text()
        )
        driver.set("chi_cost.S:value", 100)
        driver.commit()
        call = {
            "sender": "validators",
            "contract_name": "chi_cost",
            "function_name": "set_value",
            "kwargs": {"new_value": ContractingDecimal("1.5")},
        }
        environment, overlay = driver.committed_storage()
        outcome = execute_tx(
            environment,
            overlay,
            **call,
            context=vm_call_context(**call, environment={}),
            meter=False,
        )
        assert outcome.status_code == 1
        assert "int" in str(outcome.result)
        assert outcome.writes == {}
        assert driver.get("chi_cost.S:value") == 100
