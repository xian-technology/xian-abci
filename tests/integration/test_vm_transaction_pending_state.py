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
    )


def _block_with_pending_state(tmp_path):
    processor, _ = make_processor(tmp_path)
    driver = processor.client.raw_driver
    driver.set("con_probe.earlier", [{"n": index} for index in range(1000)])
    driver.set_event({"event": "Earlier"})
    return driver, driver.pending_writes["con_probe.earlier"]


def test_execution_keeps_earlier_block_state_in_place(tmp_path):
    driver, earlier_value = _block_with_pending_state(tmp_path)
    pending_writes, pending_reads = driver.pending_writes, driver.pending_reads
    try:
        outcome = _run(driver)

        assert outcome.output.status_code == 0, outcome.output.result
        assert driver.pending_writes is pending_writes
        assert driver.pending_reads is pending_reads
        assert driver.pending_writes["con_probe.earlier"] is earlier_value
        assert driver.log_events == [{"event": "Earlier"}]
    finally:
        driver.close()


def test_execution_effects_are_returned_not_left_on_the_driver(tmp_path):
    driver, _ = _block_with_pending_state(tmp_path)
    try:
        writes_before = dict(driver.pending_writes)

        outcome = _run(driver)

        assert "con_probe.value" in outcome.writes
        assert "currency.balances:alice" in outcome.writes
        assert dict(driver.pending_writes) == writes_before
    finally:
        driver.close()


def test_failed_execution_leaves_only_its_reads_out_of_the_block(tmp_path):
    driver, _ = _block_with_pending_state(tmp_path)
    try:
        writes_before = dict(driver.pending_writes)
        reads_before = dict(driver.pending_reads)

        outcome = _run(driver, function="spend", budget=1)

        assert outcome.output.status_code == 1
        assert dict(driver.pending_writes) == writes_before
        assert set(driver.pending_reads) - set(reads_before) <= {"currency.balances:alice"}
    finally:
        driver.close()

