"""One failed-receipt policy for every user transaction that cannot run.

Failures before or around contract execution must be deterministic receipts
that charge the transaction base chi, pay that fee out to the validators and
consume the nonce. (Host faults halt the node: see test_chain_chi_rate.)
"""

from contracting.constants import TRANSACTION_BASE_CHI
from fee_probe_support import make_processor, process, tx
from xian_runtime_types.decimal import ContractingDecimal

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
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(processor, tx(contract="con_missing", function="any"))
        result = _assert_base_charge_receipt(response, driver)
        assert "con_missing" in result["result"]
    finally:
        driver.close()


def test_dangling_import_fails_identically_on_cold_and_warm_processes(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        driver.set_contract_from_source(
            "con_dangling",
            "import con_absent\n\n@export\ndef ping():\n    return 1\n",
        )
        driver.commit()
        cold = process(processor, tx(contract="con_dangling", function="ping"))
        driver.commit()
        warm = process(processor, tx(contract="con_dangling", function="ping"))
        assert cold["tx_result"]["status"] == warm["tx_result"]["status"] == 1
        assert cold["tx_result"]["chi_used"] == warm["tx_result"]["chi_used"]
        assert cold["tx_result"]["result"] == warm["tx_result"]["result"]
        assert "con_absent" in cold["tx_result"]["result"]
    finally:
        driver.close()


def test_failed_receipt_charge_never_exceeds_the_submitted_budget(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(processor, tx(contract="con_missing", budget=3))
        _assert_base_charge_receipt(response, driver, chi=3)
    finally:
        driver.close()


def test_unmetered_system_failure_is_not_charged(tmp_path):
    processor = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        response = process(
            processor,
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
