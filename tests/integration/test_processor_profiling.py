"""Block executor profiling preserves transaction receipts and reports phase timings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import test_block_receipt_values as receipts

from xian import processor as processor_module
from xian.perf import PerfTracker
from xian.processor import TxProcessor

pytest.importorskip("xian_vm_core")
receipt_chain = receipts.chain


@pytest.fixture
def profiler(tmp_path):
    return PerfTracker(
        output_path=tmp_path / "perf.json", node_name="n", chain_id="c", execution_mode="m"
    )


def test_block_execution_reports_phase_timings_without_changing_receipts(receipt_chain, profiler):
    client, unprofiled = receipt_chain
    processor = TxProcessor(client=client, profiler=profiler)
    expected = receipts._receipt((client, unprofiled), "members")
    expected_writes = client.raw_driver.pending_stored_items()
    client.raw_driver.rollback()
    profiler.start_block(1, 2)
    assert receipts._receipt((client, processor), "members") == expected
    assert client.raw_driver.pending_stored_items() == expected_writes
    # A system transaction (epoch rebalance, evidence) is block work too.
    processor.process_system_tx(
        {
            "payload": {
                "chain_id": "xian-local",
                "chi_supplied": 0,
                "contract": "con_values",
                "function": "members",
                "kwargs": {},
                "nonce": 0,
                "sender": "sys",
            },
            "metadata": {"signature": "e" * 128},
            "b_meta": {
                "nanos": 1_790_000_000_000_000_000,
                "height": 1,
                "hash": "0" * 64,
                "chain_id": "xian-local",
            },
        }
    )
    metrics = profiler.active_block.metrics
    total = metrics["tx_process_total"]
    parts = [
        metrics[name]
        for name in (
            "tx_native_prepare",
            "tx_native_execute",
            "tx_native_to_python",
            "tx_receipt_conversion",
        )
    ]
    # The user batch and the system transaction contribute one observation each.
    assert total.count == profiler.global_metrics["tx_process_total"].count == 2
    assert all(part.count == 2 and part.total_ns > 0 for part in parts)
    assert sum(part.total_ns for part in parts) <= total.total_ns


def test_profiling_still_executes_on_a_wheel_without_native_timings(
    receipt_chain, profiler, monkeypatch
):
    client, _ = receipt_chain
    native_executor = processor_module.BlockExecutor

    def legacy_executor(*args, **kwargs):
        executor = native_executor(*args, **kwargs)
        return SimpleNamespace(add=executor.add, run=executor.run)

    monkeypatch.setattr(processor_module, "BlockExecutor", legacy_executor)
    processor = TxProcessor(client=client, profiler=profiler)
    assert receipts._receipt((client, processor), "fail")["status"] == 1
    assert profiler.global_metrics.keys() == {
        "tx_process_total",
        "tx_native_prepare",
        "tx_receipt_conversion",
    }
