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
