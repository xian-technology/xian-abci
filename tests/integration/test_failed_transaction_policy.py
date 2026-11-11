"""One failed-receipt policy for every user transaction that cannot run.

Failures before or around contract execution must be deterministic receipts
that charge the transaction base chi and consume the nonce. Host faults whose
outcome depends on the machine must halt the node instead of becoming
receipts.
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


def _assert_base_charge_receipt(response, driver, *, chi=TRANSACTION_BASE_CHI):
    result = response["tx_result"]
    assert result is not None
    assert result["status"] == 1
    assert result["chi_used"] == chi
    assert result["events"] == []
    assert result["rewards"] is None
    assert result["state"] == [
        {"key": "currency.balances:alice", "value": _balance_after_base_charge(chi=chi)}
    ]
    assert driver.get("currency.balances:alice") == _balance_after_base_charge(chi=chi)
    assert response["access"] is not None
    assert "currency.balances:alice" in response["access"].writes
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
        with patch.object(
            rewards,
            "build_tx_reward_outputs",
            side_effect=ArithmeticError("reward overflow"),
        ):
            response = process(processor, rewards, tx())
        result = _assert_base_charge_receipt(response, driver)
        assert result["result"] == repr(RuntimeError(HOST_FAILURE_MESSAGE))
        assert driver.get("con_probe.value") is None
    finally:
        driver.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_parallel_matches_serial_for_deploy_then_call_and_missing_contract(tmp_path, workers):
    from copy import deepcopy

    from xian_runtime_types.time import Datetime

    from xian.fee_policy import TxFeePolicy
    from xian.genesis_builder import _stage_native_submission_contract
    from xian.parallel_executor import ParallelBlockExecutor

    processors = {}
    for name in ("serial", "parallel"):
        processor, rewards = make_processor(tmp_path / name)
        driver = processor.client.raw_driver
        _stage_native_submission_contract(driver, submission_time=Datetime(2026, 1, 1))
        driver.set("currency.balances:bob", 100)
        driver.commit()
        processors[name] = (processor, rewards)

    source = "@export\ndef ping():\n    return 7\n"
    transactions = [
        tx(
            contract="submission",
            function="submit_contract",
            kwargs={"name": "con_fresh", "code": source},
            budget=5000,
        ),
        tx(sender="bob", contract="con_fresh", function="ping"),
        tx(sender="bob", contract="con_missing", function="pong"),
    ]
    serial, serial_rewards = processors["serial"]
    parallel, parallel_rewards = processors["parallel"]
    executor = ParallelBlockExecutor(
        storage_home=tmp_path / "parallel",
        enabled=True,
        workers=workers,
        min_transactions=1,
        access_estimates_enabled=False,
    )
    try:
        expected = [process(serial, serial_rewards, t) for t in deepcopy(transactions)]
        actual, _stats = executor.execute(
            txs=deepcopy(transactions),
            tx_processor=parallel,
            fee_policy=TxFeePolicy.paid_metered(),
            rewards_handler=parallel_rewards,
        )
        assert [r["tx_result"] for r in actual] == [r["tx_result"] for r in expected]
        assert [r["tx_result"]["status"] for r in expected] == [0, 0, 1]
        assert expected[2]["tx_result"]["chi_used"] == TRANSACTION_BASE_CHI
        for key in ("currency.balances:alice", "currency.balances:bob"):
            assert parallel.client.raw_driver.get(key) == serial.client.raw_driver.get(key)
    finally:
        executor.close()
        serial.client.raw_driver.close()
        parallel.client.raw_driver.close()


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
