"""Block executor profiling preserves transaction receipts and reports phase timings."""

from __future__ import annotations

import pytest
import test_block_receipt_values as receipts

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
    expected = receipts._receipt_bytes((client, unprofiled), "members")
    expected_writes = client.raw_driver.pending_stored_items()
    client.raw_driver.rollback()
    profiler.start_block(1, 2)
    assert receipts._receipt_bytes((client, processor), "members") == expected
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
    assert metrics["tx_native_receipt_encoding"].count == 1
    assert sum(part.total_ns for part in parts) <= total.total_ns
