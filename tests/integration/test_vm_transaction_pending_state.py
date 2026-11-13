"""A transaction undoes its execution-time changes without copying the block.

Earlier transactions of a block leave pending writes on the driver. Running one
more transaction must not deep-copy that state twice (its cost grew with the
block); it must undo exactly what the execution touched and leave the rest of
the block's pending state as it was.
"""

from fee_probe_support import make_processor

from xian.execution_engine import VmRuntime, execute_vm_transaction


def _run(driver, *, function="record", budget=1000):
    return execute_vm_transaction(
        VmRuntime(),
        driver,
        sender="alice",
        contract_name="con_probe",
        function_name=function,
        kwargs={"amount": 1} if function == "spend" else {},
        environment={"chain_id": "local"},
        chi_budget=budget,
        chi_cost=100,
        meter=True,
        transaction_size_bytes=0,
    )


def _block_with_pending_state(tmp_path):
    processor, _ = make_processor(tmp_path)
    driver = processor.client.raw_driver
    driver.set("con_probe.earlier", [{"n": index} for index in range(1000)])
    driver.set_event({"event": "Earlier"})
    return driver


def test_execution_keeps_earlier_block_state_in_place(tmp_path):
    driver = _block_with_pending_state(tmp_path)
    pending = driver._pending
    earlier = pending.lookup("con_probe.earlier")
    try:
        outcome = _run(driver)

        assert outcome.output.status_code == 0, outcome.output.result
        assert driver._pending is pending
        assert pending.lookup("con_probe.earlier") == earlier
        assert driver.log_events == [{"event": "Earlier"}]
    finally:
        driver.close()


def test_execution_effects_are_returned_not_left_on_the_driver(tmp_path):
    driver = _block_with_pending_state(tmp_path)
    try:
        writes_before = driver.pending_items()

        outcome = _run(driver)

        assert "con_probe.value" in outcome.writes
        assert "currency.balances:alice" in outcome.writes
        assert driver.pending_items() == writes_before
    finally:
        driver.close()


def test_failed_execution_leaves_the_block_state_as_it_was(tmp_path):
    driver = _block_with_pending_state(tmp_path)
    try:
        before = (driver.pending_items(), driver.transaction_access())

        outcome = _run(driver, function="spend", budget=1)

        assert outcome.output.status_code == 1
        assert (driver.pending_items(), driver.transaction_access()) == before
    finally:
        driver.close()

