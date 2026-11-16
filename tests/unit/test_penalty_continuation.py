from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from xian.methods import finalize_block
from xian.perf import NoopPerfTracker


def app_with_pending(head):
    return SimpleNamespace(
        client=SimpleNamespace(raw_driver=SimpleNamespace(get=Mock(return_value=head))),
        tx_processor=SimpleNamespace(process_system_tx=Mock()),
        current_block_meta={"height": 7, "hash": "ab" * 32},
    )


def test_no_pending_penalty_does_not_execute_a_system_transaction():
    from xian.methods._penalties import maybe_process_pending_slashes

    app = app_with_pending(None)

    assert maybe_process_pending_slashes(app, height=7) is False
    app.tx_processor.process_system_tx.assert_not_called()
    app.client.raw_driver.get.assert_called_once_with("validator_penalties.pending_head")


@pytest.mark.parametrize("head", [0, 9])
def test_one_pending_queue_causes_exactly_one_bounded_contract_call(head):
    from xian.methods._penalties import maybe_process_pending_slashes

    app = app_with_pending(head)
    app.tx_processor.process_system_tx.return_value = {
        "tx_result": {"status": 0, "state": [{"key": "validator_penalties.pending_head", "value": 10}]}
    }

    assert maybe_process_pending_slashes(app, height=7) is True
    app.tx_processor.process_system_tx.assert_called_once()
    transaction = app.tx_processor.process_system_tx.call_args.args[0]
    assert transaction["payload"] == {
        "sender": "__penalty_driver__",
        "contract": "validator_penalties",
        "function": "process_pending_slashes",
        "kwargs": {},
        "chi_supplied": 0,
    }
    assert transaction["metadata"]["signature"] == "validator-penalty-batch:7"
    assert transaction["b_meta"] == app.current_block_meta


@pytest.mark.parametrize("result", [{}, {"tx_result": {"status": 1}}, {"tx_result": {"status": 0, "state": []}}])
def test_failed_or_empty_batch_is_retryable_next_block(result):
    from xian.methods._penalties import maybe_process_pending_slashes

    app = app_with_pending(1)
    app.tx_processor.process_system_tx.return_value = result

    assert maybe_process_pending_slashes(app, height=7) is False
    assert maybe_process_pending_slashes(app, height=8) is False
    assert app.tx_processor.process_system_tx.call_count == 2


def test_pending_penalties_run_after_new_evidence_and_liveness_before_epoch_selection():
    app = SimpleNamespace(profiler=NoopPerfTracker())
    order = []
    with (
        patch.object(finalize_block, "maybe_apply_evidence_penalties", side_effect=lambda *a, **k: order.append("evidence")),
        patch.object(finalize_block, "maybe_record_liveness", side_effect=lambda *a, **k: order.append("liveness")),
        patch.object(finalize_block, "maybe_process_pending_slashes", side_effect=lambda *a, **k: order.append("penalties")),
        patch.object(finalize_block, "_maybe_run_validator_epoch_rebalance", side_effect=lambda *a, **k: order.append("epoch")),
    ):
        finalize_block._apply_evidence_and_liveness(app, object(), height=7)
        order.append("paid_transactions")
        finalize_block._continue_penalties_and_rebalance(app, height=7)

    assert order == ["evidence", "liveness", "paid_transactions", "penalties", "epoch"]
