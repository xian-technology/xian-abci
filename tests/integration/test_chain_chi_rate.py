"""The chi rate is chain state: every node path reads it the same way.

Genesis seeds chi_cost.S:value. Admission (CheckTx), execution and
simulation all price fees with it, so a missing or invalid rate is a broken
chain: the node refuses to guess a price (it used to guess 1 in execution and
20 in admission and simulation, so they could disagree).
"""

from types import SimpleNamespace

import pytest
from fee_probe_support import make_processor, process, tx

from xian.exceptions import TransactionException
from xian.fee_policy import TxFeePolicy, chain_chi_rate
from xian.simulator import TransactionSimulator
from xian.utils.tx import validate_transaction_after_static

RATE_KEY = "chi_cost.S:value"


class _Client:
    def __init__(self, value):
        self.value = value

    def get_var(self, contract, variable, arguments=None, mark=True):
        assert (contract, variable, arguments) == ("chi_cost", "S", ["value"])
        return self.value


def test_the_rate_is_read_from_chain_state():
    assert chain_chi_rate(_Client(20)) == 20


@pytest.mark.parametrize("value", [None, 0, -5, True, "20", 1.5])
def test_a_missing_or_invalid_rate_is_refused(value):
    with pytest.raises(ValueError, match="chi_cost.S:value"):
        chain_chi_rate(_Client(value))


def _processor_without_rate(tmp_path):
    processor, rewards = make_processor(tmp_path)
    driver = processor.client.raw_driver
    driver.delete(RATE_KEY)
    driver.commit()
    return processor, rewards, driver


def test_paid_execution_refuses_to_price_fees_without_a_rate(tmp_path):
    processor, rewards, driver = _processor_without_rate(tmp_path)
    try:
        with pytest.raises(ValueError, match="chi_cost.S:value"):
            process(processor, rewards, tx())
        assert driver.get("currency.balances:alice") == 100
    finally:
        driver.close()


def test_free_execution_does_not_need_a_rate(tmp_path):
    processor, rewards, driver = _processor_without_rate(tmp_path)
    try:
        response = process(processor, rewards, tx(), policy=TxFeePolicy.free_metered())
        assert response["tx_result"]["status"] == 0
    finally:
        driver.close()


def test_admission_refuses_to_price_fees_without_a_rate(tmp_path):
    processor, _rewards, driver = _processor_without_rate(tmp_path)
    try:
        with pytest.raises(TransactionException, match="chi_cost.S:value"):
            validate_transaction_after_static(
                processor.client,
                SimpleNamespace(check_nonce=lambda *_a, **_k: None),
                tx(),
                tx_hash="ab",
                fee_policy=TxFeePolicy.paid_metered(),
            )
    finally:
        driver.close()


def test_paid_simulation_reports_a_missing_rate(tmp_path):
    processor, _rewards, driver = _processor_without_rate(tmp_path)
    try:
        simulator = TransactionSimulator(client=processor.client, chain_id="local")
        result = simulator.simulate(
            {"sender": "alice", "contract": "con_probe", "function": "record", "kwargs": {}}
        )
        assert result["status"] == 1
        assert "chi_cost.S:value" in str(result["result"])
    finally:
        driver.close()
