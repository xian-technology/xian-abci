"""One failed-receipt policy for every user transaction that cannot run.

Failures before or around contract execution must be deterministic receipts
that charge the transaction base chi, pay that fee out to the validators and
consume the nonce. Host faults whose outcome depends on the machine must halt
the node instead of becoming receipts.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from fee_probe_support import make_processor, process, tx
from xian_runtime_types.decimal import ContractingDecimal

from xian.execution_engine import HOST_FAILURE_MESSAGE, clear_prepared_contract_cache
from xian.fee_policy import TxFeePolicy

RATE = 100


def _balance_after_base_charge(balance=100, chi=TRANSACTION_BASE_CHI):
    return balance - ContractingDecimal(chi) / RATE


def _assert_base_charge_receipt(response, driver, *, chi=TRANSACTION_BASE_CHI, paid_out=True):
    """The base charge, paid out whole to the one validator (split 1/0/0/0)."""
    result = response["tx_result"]
    assert result is not None
    assert result["status"] == 1
    assert result["chi_used"] == chi
    assert result["events"] == []
    charged = {"key": "currency.balances:alice", "value": _balance_after_base_charge(chi=chi)}
    fee = ContractingDecimal(chi) / RATE
    if paid_out:
        assert result["rewards"]["validator_reward"] == {"validator": fee}
        assert result["state"] == [charged, {"key": "currency.balances:validator", "value": fee}]
        assert driver.get("currency.balances:validator") == fee
    else:
        assert result["rewards"] is None
        assert result["state"] == [charged]
        assert driver.get("currency.balances:validator") is None
    assert driver.get("currency.balances:alice") == _balance_after_base_charge(chi=chi)
    return result


def test_missing_contract_is_a_failed_receipt_charging_base_chi(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(processor, rewards, tx(contract="con_missing", function="any"))
        result = _assert_base_charge_receipt(response, driver)
        assert "con_missing" in result["result"]
    finally:
        driver.close()


def test_dangling_import_fails_identically_on_cold_and_warm_processes(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source(
            "con_dangling",
            "import con_absent\n\n@export\ndef ping():\n    return 1\n",
        )
        driver.commit()
        clear_prepared_contract_cache()
        cold = process(processor, rewards, tx(contract="con_dangling", function="ping"))
        driver.commit()
        warm = process(processor, rewards, tx(contract="con_dangling", function="ping"))
        assert cold["tx_result"]["status"] == warm["tx_result"]["status"] == 1
        assert cold["tx_result"]["chi_used"] == warm["tx_result"]["chi_used"]
        assert cold["tx_result"]["result"] == warm["tx_result"]["result"]
        assert "con_absent" in cold["tx_result"]["result"]
    finally:
        driver.close()


def test_failed_receipt_charge_never_exceeds_the_submitted_budget(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(processor, rewards, tx(contract="con_missing", budget=3))
        _assert_base_charge_receipt(response, driver, chi=3)
    finally:
        driver.close()


def test_unexpected_vm_boundary_error_gets_canonical_message_and_base_charge(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        bindings = __import__("xian_vm_core")
        with patch.object(
            bindings,
            "execute_contract",
            side_effect=RuntimeError("machine specific detail at 0x7f00"),
        ):
            response = process(processor, rewards, tx())
        result = _assert_base_charge_receipt(response, driver)
        assert result["result"] == repr(RuntimeError(HOST_FAILURE_MESSAGE))
        assert driver.get("con_probe.value") is None
    finally:
        driver.close()


@pytest.mark.parametrize("fault", [MemoryError(), OSError(5, "I/O error")])
def test_host_faults_halt_instead_of_becoming_receipts(tmp_path, fault):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        bindings = __import__("xian_vm_core")
        with patch.object(bindings, "execute_contract", side_effect=fault):
            with pytest.raises(type(fault)):
                process(processor, rewards, tx())
        assert driver.get("currency.balances:alice") == 100
        assert driver.get("con_probe.value") is None
    finally:
        driver.close()


def test_post_execution_processing_error_becomes_failed_receipt(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        # The failed receipt's own payout raises too: its fee is charged
        # without a payout rather than halting the chain.
        with patch.object(
            rewards,
            "build_tx_reward_outputs",
            side_effect=ArithmeticError("reward overflow"),
        ):
            response = process(processor, rewards, tx())
        result = _assert_base_charge_receipt(response, driver, paid_out=False)
        assert result["result"] == repr(RuntimeError(HOST_FAILURE_MESSAGE))
        assert driver.get("con_probe.value") is None
    finally:
        driver.close()


def test_host_fault_while_paying_out_a_failed_receipt_halts(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        with patch.object(
            rewards,
            "build_tx_reward_outputs",
            side_effect=[ArithmeticError("reward overflow"), MemoryError()],
        ):
            with pytest.raises(MemoryError):
                process(processor, rewards, tx())
        assert driver.get("currency.balances:alice") == 100
        assert driver.get("currency.balances:validator") is None
    finally:
        driver.close()


def test_unmetered_system_failure_is_not_charged(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(
            processor,
            rewards,
            tx(contract="con_missing", function="any"),
            policy=TxFeePolicy.system_unmetered(),
        )
        result = response["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == 0
        assert result["state"] == []
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


def test_successful_transaction_keeps_its_measured_chi(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        with patch.object(
            __import__("xian_vm_core"),
            "execute_contract",
            return_value=SimpleNamespace(
                status_code=0,
                result=None,
                writes={},
                events=[],
                chi_used=2,
                contract_costs={},
            ),
        ):
            result = process(processor, rewards, tx())["tx_result"]
        assert result["status"] == 0
        assert result["chi_used"] == 2
    finally:
        driver.close()
