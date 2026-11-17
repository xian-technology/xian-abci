"""Actual immutable registry calls through the production Finalize/Commit path."""

import asyncio
from pathlib import Path

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from test_evidence_retention import app as app
from test_stake_time_anchors import _finalize, _receipt
from xian_runtime_types.time import Datetime

from tendermint.abci.types_pb2 import RequestFinalizeBlock
from xian.config_paths import resolve_contracts_dir
from xian.state_root import compute_driver_state_root
from xian.xian_abci import Xian


def test_native_registry_requires_keeper_deduplicates_and_preserves_anchor_after_restart(
    app, request
):
    source = Path(resolve_contracts_dir()) / "time_checkpoints.s.py"
    if not app.client.raw_driver.has_contract("time_checkpoints"):
        app.client.submit(source.read_text(), name="time_checkpoints")
    registry = app.client.get_contract_proxy("time_checkpoints")
    assert registry.get_protocol_version() == 1
    with pytest.raises(AssertionError, match="keeper"):
        registry.request_anchor(signer="timer", environment={"block_num": 1})

    native_process = app.tx_processor.process_block

    def paid(*args, **kwargs):
        if kwargs["fee_policy"].mode == "system_unmetered":
            return native_process(*args, **kwargs)
        # Initialization is a real native system call before this paid boundary.
        assert app.client.raw_driver.get("time_checkpoints.keeper_ready") is True
        context = {"block_num": 1, "now": Datetime(1970, 1, 11)}
        assert registry.request_anchor(signer="timer", environment=context) == 1
        assert registry.request_anchor(signer="another", environment=context) == 1
        assert registry.get_anchor(height=1) is None
        assert app.client.raw_driver.get("time_checkpoints.next_job_id") == 2
        return [_receipt()]

    _finalize(app, height=1, seconds=10 * 86400 + 5, paid=paid)
    asyncio.run(app.commit())
    assert registry.get_anchor(height=1) is None
    anchored = asyncio.run(
        app.finalize_block(RequestFinalizeBlock(height=2, time=Timestamp(seconds=100 * 86400)))
    )
    expected = anchored.SerializeToString(deterministic=True)
    # Lose a successful Finalize before Commit; the immutable anchor must replay.
    app._discard_pending_commit_driver_state()
    app = _restart(app, request)
    registry = app.client.get_contract_proxy("time_checkpoints")
    assert registry.get_anchor(height=1) is None
    anchored = asyncio.run(
        app.finalize_block(RequestFinalizeBlock(height=2, time=Timestamp(seconds=100 * 86400)))
    )
    assert anchored.SerializeToString(deterministic=True) == expected
    asyncio.run(app.commit())
    assert registry.get_anchor(height=1) == Datetime(1970, 4, 11)
    assert app.client.raw_driver.get("time_checkpoints.queue_head") == 0
    assert anchored.app_hash == compute_driver_state_root(app.client.raw_driver)
    # A rooted native checkpoint survives ordinary restart without replaying work.
    restarted = _restart(app, request)
    registry = restarted.client.get_contract_proxy("time_checkpoints")
    settled = asyncio.run(
        restarted.finalize_block(
            RequestFinalizeBlock(height=3, time=Timestamp(seconds=100 * 86400 + 1))
        )
    )
    asyncio.run(restarted.commit())
    assert settled.app_hash == anchored.app_hash
    assert registry.get_anchor(height=1) == Datetime(1970, 4, 11)
    assert restarted.client.raw_driver.get("time_checkpoints.keeper_ready") is True


def _restart(app, request):
    asyncio.run(app.close())
    app.client.close()
    restarted = Xian(constants=app.constants)
    request.addfinalizer(restarted.client.close)
    request.addfinalizer(lambda: asyncio.run(restarted.close()))
    return restarted
