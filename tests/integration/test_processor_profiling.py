"""TxProcessor reports the time a block's transactions take as
`tx_process_total` (the perf summary subtracts it from `finalize_block`)."""

from __future__ import annotations

import pytest
from test_block_receipt_values import _receipt, chain  # noqa: F401 - the fixture

from xian.processor import TxProcessor

pytest.importorskip("xian_vm_core")


def test_block_execution_is_profiled_as_tx_process_total(chain, tmp_path):
    from xian.perf import PerfTracker

    client, _ = chain
    profiler = PerfTracker(
        output_path=tmp_path / "perf.json", node_name="n", chain_id="c", execution_mode="m"
    )
    processor = TxProcessor(client=client, profiler=profiler)
    profiler.start_block(1, 2)
    _receipt((client, processor), "members")
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
    metric = profiler.active_block.metrics["tx_process_total"]

    # Once per execution of a block's transactions (here: the block, then
    # the system transaction), timing execution and the receipts.
    assert metric.count == 2
    assert metric.total_ns > 0
    assert profiler.global_metrics["tx_process_total"].count == 2
