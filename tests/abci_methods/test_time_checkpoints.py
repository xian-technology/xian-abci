"""Fixed trusted clock keeper must become ready before a paid timer request."""

import asyncio
from unittest.mock import patch

import pytest
from test_evidence_retention import app as app
from test_stake_time_anchors import _finalize, _receipt

PREFIX = "time_checkpoints."


def _seed(driver, *, ready=False, head=0):
    driver.set(PREFIX + "protocol_version", 1)
    driver.set(PREFIX + "keeper_ready", ready)
    driver.set(PREFIX + "queue_head", head)
    driver.commit()


def test_empty_service_initializes_once_before_staking_and_paid_then_quiesces(app):
    driver = app.client.raw_driver
    _seed(driver)
    events = []

    def system(tx):
        events.append("keeper")
        assert tx["payload"] == {
            "sender": "__time_checkpoints__",
            "contract": "time_checkpoints",
            "function": "process_time_anchors",
            "kwargs": {"max_work": 32},
            "chi_supplied": 0,
        }
        assert tx["b_meta"]["height"] == 1
        driver.set(PREFIX + "keeper_ready", True)
        return {"tx_result": {"status": 0}}

    def paid(*_args, **_kwargs):
        events.append("paid")
        assert driver.get(PREFIX + "keeper_ready") is True
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
        patch(
            "xian.methods.finalize_block.maybe_process_time_anchors",
            side_effect=lambda *a, **kw: events.append("staking"),
        ),
        patch.object(app.tx_processor, "process_system_tx", side_effect=system),
    ):
        first = _finalize(app, height=1, seconds=10 * 86400 + 5, paid=paid)
    assert events == ["evidence", "liveness", "keeper", "staking", "paid"]
    asyncio.run(app.commit())
    with patch.object(
        app.tx_processor,
        "process_system_tx",
        side_effect=AssertionError("ready empty keeper churn"),
    ):
        second = _finalize(app, height=2, seconds=10 * 86400 + 6)
    assert second.app_hash == first.app_hash


@pytest.mark.parametrize("requested_height", [1, 100])
def test_keeper_only_anchors_older_heads_with_one_bounded_call(app, requested_height):
    from xian.methods._time_checkpoints import maybe_process_time_checkpoints

    driver = app.client.raw_driver
    _seed(driver, ready=True, head=9)
    driver.set(PREFIX + "job_heights:9", requested_height)
    app.current_block_meta = {"height": 100, "hash": "h", "nanos": 1}
    with patch.object(
        app.tx_processor, "process_system_tx", return_value={"tx_result": {"status": 0}}
    ) as call:
        maybe_process_time_checkpoints(app, height=requested_height)
        call.assert_not_called()
        maybe_process_time_checkpoints(app, height=requested_height - 1)
        call.assert_not_called()
        maybe_process_time_checkpoints(app, height=requested_height + 1)
        call.assert_called_once()
        assert call.call_args.args[0]["payload"]["kwargs"] == {"max_work": 32}


def test_missing_universal_service_is_skipped_but_partial_or_malformed_abi_refused(app):
    from xian.methods._time_checkpoints import maybe_process_time_checkpoints

    driver = app.client.raw_driver
    with patch.object(
        app.tx_processor, "process_system_tx", side_effect=AssertionError("invalid ABI ran")
    ):
        maybe_process_time_checkpoints(app, height=1)
        for key, value in [
            ("protocol_version", True),
            ("protocol_version", 2),
            ("keeper_ready", 0),
            ("queue_head", True),
            ("queue_head", False),
            ("queue_head", -1),
            ("job_heights:1", 0),
            ("job_heights:1", True),
            ("job_heights:1", None),
        ]:
            _seed(driver, ready=True, head=1)
            driver.set(PREFIX + "job_heights:1", 1)
            driver.set(PREFIX + key, value)
            with pytest.raises(RuntimeError, match="time-checkpoint"):
                maybe_process_time_checkpoints(app, height=2)


def test_failed_keeper_does_not_claim_readiness_or_start_a_clock(app):
    from xian.methods._time_checkpoints import maybe_process_time_checkpoints

    driver = app.client.raw_driver
    _seed(driver)
    app.current_block_meta = {"height": 1, "hash": "h", "nanos": 1}
    for status in (1, None):
        with patch.object(
            app.tx_processor, "process_system_tx", return_value={"tx_result": {"status": status}}
        ):
            maybe_process_time_checkpoints(app, height=1)
        assert driver.get(PREFIX + "keeper_ready") is False
