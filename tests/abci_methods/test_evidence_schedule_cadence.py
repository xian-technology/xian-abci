"""Actual empty Finalize/Commit paths must leave rooted schedules unchanged."""

import asyncio
import json
from unittest.mock import patch

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from test_evidence_retention import _evidence_query, _paid_policy_block
from test_evidence_retention import app as app

from tendermint.abci.types_pb2 import RequestFinalizeBlock
from xian.evidence_policy import DAY_NS, EVIDENCE_POLICY_KEY
from xian.methods.finalize_block import finalize_block


def test_idle_empty_blocks_keep_the_same_app_hash_and_checkpoint_time_still_advances(app):
    # The first fixture block materializes its initial validator consensus map;
    # that real state change legitimately earns a proof block.
    baseline = _paid_policy_block(app, [], height=1, seconds=10 * 86400 + 1)
    asyncio.run(app.commit())
    before = baseline.app_hash
    for height, seconds in enumerate((10 * 86400 + 5, 11 * 86400), start=2):
        result = _paid_policy_block(app, [], height=height, seconds=seconds)
        assert result.app_hash == before
        asyncio.run(app.commit())
        body = json.loads(_evidence_query(app).value)
        assert body["height"] == str(height)
        assert body["last_block_nanos"] == str(seconds * 1_000_000_000)
        assert body["evidence"]["max_age_duration"] == str(8 * DAY_NS)


def test_pending_and_saturated_ramps_emit_full_derived_limits_without_proof_hash_churn(app):
    adopted = _paid_policy_block(app, [14])
    asyncio.run(app.commit())
    raw = app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)
    assert raw["version"] == 2
    for height, elapsed in enumerate((1, 86400, 10 * 86400, 14 * 86400), start=2):
        seconds = 10 * 86400 + 5 + elapsed
        block = _paid_policy_block(app, [], height=height, seconds=seconds)
        assert block.app_hash == adopted.app_hash
        expected = min(15 * DAY_NS, 8 * DAY_NS + elapsed * 1_000_000_000)
        limits = block.consensus_param_updates.evidence
        assert (
            limits.max_age_duration.seconds * 1_000_000_000 + limits.max_age_duration.nanos
            == expected
        )
        assert limits.max_age_num_blocks == 100000 and limits.max_bytes == 1048576
        assert [field.name for field, _ in block.consensus_param_updates.ListFields()] == [
            "evidence"
        ]
        asyncio.run(app.commit())
        assert app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY) == raw
        assert json.loads(_evidence_query(app).value)["evidence"]["max_age_duration"] == str(
            expected
        )


@pytest.mark.parametrize("invalid", ["backwards", "noncanonical", "out_of_range"])
def test_invalid_finalization_clock_is_rejected_before_execution(app, invalid):
    _paid_policy_block(app, [])
    asyncio.run(app.commit())
    timestamp = Timestamp(seconds=10 * 86400 + 4 if invalid == "backwards" else 10 * 86400 + 6)
    if invalid == "noncanonical":
        timestamp.nanos = 1_000_000_000
    if invalid == "out_of_range":
        timestamp.seconds = 253402300800
    before = app.client.raw_driver.value_from_disk("__latest_block")
    with patch.object(
        app.tx_processor, "process_block", side_effect=AssertionError("executed bad clock")
    ):
        with pytest.raises(ValueError):
            asyncio.run(finalize_block(app, RequestFinalizeBlock(height=2, time=timestamp)))
    assert app.client.raw_driver.value_from_disk("__latest_block") == before


def test_long_idle_jump_rotates_once_then_proof_successor_quiesces(app):
    baseline = _paid_policy_block(app, [], height=1, seconds=10 * 86400 + 1)
    asyncio.run(app.commit())
    jumped = _paid_policy_block(app, [], height=2, seconds=100 * 86400)
    assert jumped.app_hash != baseline.app_hash
    asyncio.run(app.commit())
    record = app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)
    assert record["security_checkpoint_height"] == 2
    assert record["previous_security_checkpoint_height"] == 0
    successor = _paid_policy_block(app, [], height=3, seconds=100 * 86400 + 1)
    assert successor.app_hash == jumped.app_hash
    asyncio.run(app.commit())
    assert app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY) == record
