"""Host ordering and finite work for successor-height staking time anchors."""

import asyncio
from unittest.mock import patch

from google.protobuf.timestamp_pb2 import Timestamp
from test_evidence_retention import app as app

from tendermint.abci.types_pb2 import RequestFinalizeBlock
from xian.methods.finalize_block import finalize_block

HEAD = "validator_penalties.time_anchor_head"
JOBS = "validator_penalties.time_anchor_jobs:"


def _receipt():
    return {"tx_result": {"hash": "paid", "status": 0, "chi_used": 1, "events": [], "state": []}}


def _finalize(app, *, height, seconds, paid=None):
    transaction = {"payload": {"sender": "alice", "nonce": 0, "chi_supplied": 100}}
    request = RequestFinalizeBlock(
        height=height,
        time=Timestamp(seconds=seconds),
        txs=[b"paid"] if paid else [],
        hash=f"block{height}".encode(),
    )
    with (
        patch(
            "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
            return_value=transaction,
        ),
        patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
        patch.object(app.nonce_storage, "set_nonce_by_tx"),
        patch.object(app.tx_processor, "process_block", side_effect=paid),
    ):
        return asyncio.run(finalize_block(app, request))


def _enqueue(driver, height):
    driver.set(HEAD, 1)
    driver.set(JOBS + "1", {"kind": "unbond", "reference": 1, "height": height})


def test_older_time_jobs_run_after_evidence_liveness_before_paid_even_with_pending_slashes(app):
    events = []
    driver = app.client.raw_driver
    _enqueue(driver, 0)
    driver.set("validator_penalties.pending_head", 99)
    driver.commit()
    app.state_root_cache.rebuild_from_driver(driver)
    seconds = 10 * 86400 + 5

    def system(tx):
        name = tx["payload"]["function"]
        if name == "process_time_anchors":
            events.append("anchors")
            assert tx["payload"] == {
                "sender": "__stake_time_driver__",
                "contract": "validator_penalties",
                "function": "process_time_anchors",
                "kwargs": {"max_work": 32},
                "chi_supplied": 0,
            }
            assert tx["b_meta"]["height"] == 1
            driver.set("test.anchored_at", tx["b_meta"]["nanos"])
            driver.set(HEAD, None)
            driver.set(JOBS + "1", None)
        else:
            events.append("slashes")
        return {
            "tx_result": {
                "status": 0,
                "state": [{"key": "test.anchored_at", "value": seconds * 1_000_000_000}],
            }
        }

    def paid(*args, **kwargs):
        events.append("paid")
        assert driver.get("test.anchored_at") == seconds * 1_000_000_000
        return [_receipt()]

    with (
        patch(
            "xian.methods.finalize_block.maybe_apply_evidence_penalties",
            side_effect=lambda *a, **kw: events.append("evidence"),
        ),
        patch(
            "xian.methods.finalize_block.maybe_record_liveness",
            side_effect=lambda *a, **kw: events.append("liveness"),
        ),
        patch.object(app.tx_processor, "process_system_tx", side_effect=system),
    ):
        _finalize(app, height=1, seconds=seconds, paid=paid)
    assert events == ["evidence", "liveness", "anchors", "paid", "slashes"]


def test_same_height_job_waits_then_queue_drains_and_empty_hashes_settle(app):
    driver = app.client.raw_driver

    def paid(*args, **kwargs):
        _enqueue(driver, 1)
        return [_receipt()]

    with patch.object(
        app.tx_processor, "process_system_tx", side_effect=AssertionError("anchored too early")
    ):
        first = _finalize(app, height=1, seconds=10 * 86400 + 5, paid=paid)
    asyncio.run(app.commit())

    def process(tx):
        driver.set(HEAD, None)
        driver.set(JOBS + "1", None)
        driver.set("test.anchored_at", tx["b_meta"]["nanos"])
        return {
            "tx_result": {
                "status": 0,
                "state": [{"key": "test.anchored_at", "value": tx["b_meta"]["nanos"]}],
            }
        }

    with patch.object(app.tx_processor, "process_system_tx", side_effect=process) as system:
        second = _finalize(app, height=2, seconds=100 * 86400)
        assert system.call_count == 1
    asyncio.run(app.commit())
    assert driver.get(HEAD) is None
    assert second.app_hash != first.app_hash
    with patch.object(
        app.tx_processor, "process_system_tx", side_effect=AssertionError("empty queue did work")
    ):
        third = _finalize(app, height=3, seconds=100 * 86400 + 1)
    assert third.app_hash == second.app_hash
    asyncio.run(app.commit())


def test_same_height_queue_is_not_rewritten_or_executed(app):
    driver = app.client.raw_driver
    _enqueue(driver, 1)
    driver.commit()
    with patch.object(
        app.tx_processor, "process_system_tx", side_effect=AssertionError("same-height job ran")
    ):
        _finalize(app, height=1, seconds=10 * 86400 + 5)
    asyncio.run(app.commit())
    assert driver.get(HEAD) == 1
    assert driver.get(JOBS + "1")["height"] == 1


def test_failed_anchor_call_keeps_pending_clock_and_does_not_grant_readiness(app):
    from xian.methods._stake_time import maybe_process_time_anchors

    driver = app.client.raw_driver
    _enqueue(driver, 0)
    app.current_block_meta = {"height": 1, "hash": "block1", "nanos": 10 * 86400 * 1_000_000_000}
    with patch.object(
        app.tx_processor,
        "process_system_tx",
        return_value={"tx_result": {"status": 1, "state": []}},
    ):
        assert not maybe_process_time_anchors(app, height=1)
    assert driver.get(HEAD) == 1 and driver.get("test.anchored_at") is None


def test_corrupt_queue_metadata_fails_closed_before_system_execution(app):
    import pytest

    from xian.methods._stake_time import maybe_process_time_anchors

    driver = app.client.raw_driver
    for head, job in [
        (0, None),
        (True, None),
        (1, None),
        (1, {"height": True}),
        (1, {"height": -1}),
    ]:
        driver.set(HEAD, head)
        driver.set(JOBS + "1", job)
        with patch.object(
            app.tx_processor, "process_system_tx", side_effect=AssertionError("corrupt job ran")
        ):
            with pytest.raises(
                RuntimeError, match="queue head" if head is True or head == 0 else "queue job"
            ):
                maybe_process_time_anchors(app, height=1)


def test_success_and_missing_or_failed_receipts_report_the_observable_outcome(app):
    from xian.methods._stake_time import maybe_process_time_anchors

    driver = app.client.raw_driver
    _enqueue(driver, 0)
    app.current_block_meta = {"height": 1, "hash": "block1", "nanos": 10 * 86400 * 1_000_000_000}
    for receipt, level in [
        ({"status": 0, "state": []}, "INFO"),
        ({"status": 1, "state": []}, "WARNING"),
        (None, "WARNING"),
    ]:
        with (
            patch.object(
                app.tx_processor, "process_system_tx", return_value={"tx_result": receipt}
            ) as execute,
            patch("xian.methods._stake_time.logger") as log,
        ):
            maybe_process_time_anchors(app, height=1)
        execute.assert_called_once()
        log.bind.return_value.log.assert_called_once_with(
            level, "Processed staking time-anchor batch"
        )
