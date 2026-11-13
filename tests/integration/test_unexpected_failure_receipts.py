"""An unexpected processing error becomes the same failed receipt in every fee mode."""

from unittest.mock import patch

import pytest
from contracting.constants import TRANSACTION_BASE_CHI
from fee_probe_support import make_processor, process, tx

from xian.execution_engine import HOST_FAILURE_MESSAGE
from xian.fee_policy import TxFeePolicy


@pytest.mark.parametrize(
    ("policy", "chi_used"),
    [
        (TxFeePolicy.system_unmetered(), 0),
        (TxFeePolicy.free_metered(), TRANSACTION_BASE_CHI),
    ],
)
def test_unexpected_error_receipt_charges_nothing_without_paid_fees(tmp_path, policy, chi_used):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    try:
        with patch.object(processor, "execute_tx", side_effect=ArithmeticError("boom")):
            response = process(processor, rewards, tx(), policy=policy)

        result = response["tx_result"]
        assert result["status"] == 1
        assert result["chi_used"] == chi_used
        assert result["result"] == repr(RuntimeError(HOST_FAILURE_MESSAGE))
        assert result["state"] == []
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


@pytest.mark.parametrize(
    "bad_write",
    [
        pytest.param({"con_probe.x" + "x" * 600: 1}, id="key-lmdb-cannot-store"),
        pytest.param({"con_probe.zz": object()}, id="unpersistable-value"),
    ],
)
def test_a_write_set_that_fails_midway_stages_none_of_its_writes(tmp_path, bad_write):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    output = {
        "status_code": 0,
        "result": None,
        # Writes are staged in key order: the valid one first, then the bad one.
        "writes": {"con_probe.value": 7, **bad_write},
        "events": [],
        "chi_used": TRANSACTION_BASE_CHI,
        "contract_costs": {"con_probe": 5_000},
    }
    try:
        with patch.object(processor, "execute_tx", return_value=output):
            response = process(processor, rewards, tx(), policy=TxFeePolicy.free_metered())

        result = response["tx_result"]
        assert result["status"] == 1
        assert result["result"] == repr(RuntimeError(HOST_FAILURE_MESSAGE))
        assert driver.get("con_probe.value") is None
        assert not any(key.startswith("con_probe.") for key, _ in driver.pending_items())
    finally:
        driver.close()
