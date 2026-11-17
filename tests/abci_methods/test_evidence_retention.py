"""Actual ABCI commit/replay boundaries for evidence retention."""

import asyncio
from unittest.mock import patch

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from xian_runtime_types.encoding import decode_wire_numbers, encode_wire, loads_wire_json

from cometbft.abci.v1beta3.types_pb2 import RequestFinalizeBlock, RequestInitChain
from xian.evidence_policy import DAY_NS, EVIDENCE_POLICY_KEY, UNBONDING_PERIOD_KEY, EvidencePolicy
from xian.methods.commit import commit
from xian.methods.finalize_block import finalize_block
from xian.methods.init_chain import init_chain
from xian.state_root import compute_exported_state_root
from xian.xian_abci import Xian


def _init_request(app):
    timestamp = Timestamp()
    timestamp.FromNanoseconds(app.genesis_evidence_policy.growth_start_nanos)
    return RequestInitChain(
        time=timestamp, consensus_params=app.genesis_evidence_policy.consensus_params()
    )


@pytest.fixture
def app(node_fixture, node_constants):
    path = node_constants.COMETBFT_GENESIS
    genesis = decode_wire_numbers(loads_wire_json(path.read_bytes()))
    policy = EvidencePolicy.seed(7, nanos=10 * DAY_NS)
    genesis["abci_genesis"]["genesis"] = [
        e for e in genesis["abci_genesis"]["genesis"] if e["key"] != EVIDENCE_POLICY_KEY
    ]
    genesis["abci_genesis"]["genesis"].append(
        {"key": EVIDENCE_POLICY_KEY, "value": policy.to_state()}
    )
    genesis["abci_genesis"]["hash"] = compute_exported_state_root(genesis["abci_genesis"]).hex()
    genesis["app_hash"] = genesis["abci_genesis"]["hash"]
    genesis["genesis_time"] = "1970-01-11T00:00:00Z"
    genesis["consensus_params"]["evidence"] = policy.comet_json()
    path.write_text(encode_wire(genesis, vm_profile="xian_vm_v2"))
    app = Xian(constants=node_constants)
    asyncio.run(init_chain(app, _init_request(app)))
    yield app
    asyncio.run(app.close())
    app.client.close()


def test_init_chain_returns_the_full_seeded_evidence_parameters(app):
    response = asyncio.run(init_chain(app, _init_request(app)))
    assert response.HasField("consensus_params")
    assert response.consensus_params.evidence.max_age_duration.seconds == 8 * 86400
    assert response.consensus_params.evidence.max_age_num_blocks == 100000
    assert response.consensus_params.evidence.max_bytes == 1048576
    assert [f.name for f, _ in response.consensus_params.ListFields()] == ["evidence"]


def test_same_block_increase_and_reduction_preserve_the_longer_claim_window(app):
    receipts = []
    for index, days in enumerate([14, 7]):
        receipts.append(
            {
                "tx_result": {
                    "hash": f"hash-{index}",
                    "status": 0,
                    "chi_used": 1,
                    "events": [],
                    "state": [{"key": UNBONDING_PERIOD_KEY, "value": days}],
                }
            }
        )
    tx = {"payload": {"sender": "alice", "nonce": 0, "chi_supplied": 100}}

    def process(*args, **kwargs):
        app.client.raw_driver.set(UNBONDING_PERIOD_KEY, 7)
        return receipts

    request = RequestFinalizeBlock(
        height=1, time=Timestamp(seconds=10 * 86400 + 5), txs=[b"a", b"b"]
    )
    with (
        patch(
            "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
            return_value=tx,
        ),
        patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
        patch.object(app.nonce_storage, "set_nonce_by_tx"),
        patch.object(app.tx_processor, "process_block", side_effect=process),
    ):
        response = asyncio.run(finalize_block(app, request))
    assert response.consensus_param_updates.evidence.max_age_duration.seconds == 8 * 86400
    assert (
        app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)["base_max_age_duration"]
        == 8 * DAY_NS
    )
    asyncio.run(commit(app))
    persisted = app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)
    assert persisted["target_max_age_duration"] == 15 * DAY_NS
    assert persisted["base_max_age_duration"] == 8 * DAY_NS
    assert persisted["growth_start_nanos"] == (10 * 86400 + 5) * 1_000_000_000
    assert app.client.raw_driver.get(UNBONDING_PERIOD_KEY) == 7
    # The app root commits the same record Comet receives.
    assert response.app_hash == app.state_root_cache.root_hash


def _paid_policy_block(app, days, *, height=1, seconds=10 * 86400 + 5, failed=False):
    tx = {"payload": {"sender": "alice", "nonce": 0, "chi_supplied": 100}}

    def process(*args, **kwargs):
        if not failed:
            app.client.raw_driver.set(UNBONDING_PERIOD_KEY, days[-1])
        return [
            {
                "tx_result": {
                    "hash": f"h-{index}",
                    "status": 1 if failed else 0,
                    "chi_used": 1,
                    "events": [],
                    "state": [{"key": UNBONDING_PERIOD_KEY, "value": value}],
                }
            }
            for index, value in enumerate(days)
        ]

    request = RequestFinalizeBlock(
        height=height, time=Timestamp(seconds=seconds), txs=[b"x"] * len(days)
    )
    with (
        patch(
            "xian.methods.finalize_block.decode_and_validate_transaction_static_bytes",
            return_value=tx,
        ),
        patch("xian.methods.finalize_block.validate_consensus_transaction_after_static"),
        patch.object(app.nonce_storage, "set_nonce_by_tx"),
        patch.object(app.tx_processor, "process_block", side_effect=process),
    ):
        return asyncio.run(finalize_block(app, request))


def test_uncommitted_growth_replays_exactly_and_committed_growth_resumes_after_restart(app):
    response = _paid_policy_block(app, [14, 7])
    expected = response.SerializeToString()
    app._discard_pending_commit_driver_state()
    asyncio.run(app.close())
    app.client.close()
    restarted = Xian(constants=app.constants)
    try:
        replayed = _paid_policy_block(restarted, [14, 7])
        assert replayed.SerializeToString() == expected
        asyncio.run(commit(restarted))
    finally:
        asyncio.run(restarted.close())
        restarted.client.close()
    resumed = Xian(constants=app.constants)
    try:
        response = _paid_policy_block(resumed, [], height=2, seconds=11 * 86400)
        asyncio.run(commit(resumed))
        assert response.consensus_param_updates.evidence.max_age_duration.seconds == 9 * 86400 - 5
        assert (
            resumed.client.raw_driver.get(EVIDENCE_POLICY_KEY)["target_max_age_duration"]
            == 15 * DAY_NS
        )
    finally:
        asyncio.run(resumed.close())
        resumed.client.close()


def test_failed_receipts_and_stable_time_cannot_bank_growth_toward_a_later_target(app):
    response = _paid_policy_block(app, [14], failed=True, seconds=20 * 86400)
    assert not response.HasField("consensus_param_updates")
    asyncio.run(commit(app))
    response = _paid_policy_block(app, [14], height=2, seconds=20 * 86400 + 3)
    assert response.consensus_param_updates.evidence.max_age_duration.seconds == 8 * 86400


def test_sequential_patch_growth_is_included_before_state_root(app):
    def apply(*args, **kwargs):
        app.client.raw_driver.set(UNBONDING_PERIOD_KEY, 7)
        return "patch-hash", [
            {"changes": [{"key": UNBONDING_PERIOD_KEY, "value": value}]} for value in [30, 7]
        ]

    with patch.object(app.state_patch_manager, "apply_patches_for_block", side_effect=apply):
        response = _paid_policy_block(app, [])
    asyncio.run(commit(app))
    assert app.client.raw_driver.get(EVIDENCE_POLICY_KEY)["target_max_age_duration"] == 31 * DAY_NS
    assert response.app_hash == app.state_root_cache.root_hash


def test_full_state_export_and_restore_preserve_both_windows_and_exact_anchor(app, tmp_path):
    from contracting.storage.driver import Driver

    from xian.state_export import export_state, import_state, load_exported_state

    _paid_policy_block(app, [14, 7])
    asyncio.run(commit(app))
    expected = app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)
    path = export_state(output_dir=tmp_path / "export", storage_home=app.constants.STORAGE_HOME)
    exported = load_exported_state(path)
    import_state(exported_state=exported, storage_home=tmp_path / "restore")
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "restore") as restored:
        assert restored.get(EVIDENCE_POLICY_KEY) == expected


@pytest.mark.parametrize("boundary", ["time", "blocks", "duration", "bytes", "missing_time"])
def test_actual_init_request_cannot_disagree_with_the_committed_genesis_policy(app, boundary):
    request = _init_request(app)
    if boundary == "time":
        request.time.nanos = 1
    elif boundary == "missing_time":
        request.ClearField("time")
    elif boundary == "blocks":
        request.consensus_params.evidence.max_age_num_blocks += 1
    elif boundary == "bytes":
        request.consensus_params.evidence.max_bytes += 1
    else:
        request.consensus_params.evidence.max_age_duration.nanos += 1
    old = app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY)
    with pytest.raises(ValueError, match="InitChain.*evidence|InitChain.*time"):
        asyncio.run(init_chain(app, request))
    assert app.client.raw_driver.value_from_disk(EVIDENCE_POLICY_KEY) == old


def test_missing_committed_record_cannot_silently_reset_the_longer_window(app):
    _paid_policy_block(app, [14, 7])
    asyncio.run(commit(app))
    app.client.raw_driver.set(EVIDENCE_POLICY_KEY, None)
    app.client.raw_driver.commit()
    with pytest.raises(ValueError, match="evidence policy"):
        Xian(constants=app.constants)


def test_finalize_requires_init_chain_record_instead_of_reseeding_from_genesis(app):
    app.client.raw_driver.set(EVIDENCE_POLICY_KEY, None)
    with pytest.raises(ValueError, match="evidence policy"):
        _paid_policy_block(app, [])


@pytest.mark.parametrize("corruption", ["anchor", "policy"])
def test_restart_checks_the_committed_anchor_and_withdrawal_target(app, corruption):
    _paid_policy_block(app, [])
    asyncio.run(commit(app))
    if corruption == "anchor":
        state = app.client.raw_driver.get(EVIDENCE_POLICY_KEY)
        state["growth_start_nanos"] = (
            app.client.raw_driver.value_from_disk("__latest_block")["nanos"] + 1
        )
        app.client.raw_driver.set(EVIDENCE_POLICY_KEY, state)
    else:
        app.client.raw_driver.set(UNBONDING_PERIOD_KEY, 30)
    app.client.raw_driver.commit()
    with pytest.raises(ValueError, match="evidence policy"):
        Xian(constants=app.constants)


def test_record_cannot_be_deleted_between_init_chain_and_first_commit(app):
    app.client.raw_driver.set(EVIDENCE_POLICY_KEY, None)
    app.client.raw_driver.commit()
    with pytest.raises(ValueError, match="evidence policy"):
        Xian(constants=app.constants)


def test_serialized_modern_genesis_wire_integer_anchor_starts_and_initializes(
    node_fixture, node_constants
):
    node = Xian(constants=node_constants)
    try:
        assert node.genesis_evidence_policy.growth_start_nanos == 1_707_351_443_342_520_258
        assert isinstance(node.genesis["abci_genesis"]["genesis"][0]["value"], dict)
        request = _init_request(node)
        response = asyncio.run(init_chain(node, request))
        assert response.app_hash.hex() == node.genesis["app_hash"]
        assert (
            node.client.raw_driver.get(EVIDENCE_POLICY_KEY)["growth_start_nanos"]
            == request.time.ToNanoseconds()
        )
    finally:
        asyncio.run(node.close())
        node.client.close()


def _evidence_query(app, *, height=0, path="/evidence_policy"):
    from io import BytesIO

    from abci.server import ProtocolHandler
    from abci.utils import read_messages
    from cometbft.abci.v1beta1.types_pb2 import RequestQuery
    from cometbft.abci.v1beta3.types_pb2 import Request, Response

    raw = asyncio.run(
        ProtocolHandler(app).process("query", Request(query=RequestQuery(path=path, height=height)))
    )
    return next(read_messages(BytesIO(raw), Response)).query


def test_evidence_query_binds_full_triple_to_the_actual_commit_point(app):
    import json

    first = _paid_policy_block(app, [])
    asyncio.run(commit(app))
    response = _evidence_query(app, height=1)
    assert response.code == 0, response.log
    assert response.height == 1
    assert response.info == "dict"
    assert response.key == b"evidence_policy"
    expected = {
        "schema_version": 1,
        "height": "1",
        "app_hash": first.app_hash.hex(),
        "last_block_nanos": str((10 * 86400 + 5) * 1_000_000_000),
        "evidence": {
            "max_age_num_blocks": "100000",
            "max_age_duration": str(8 * DAY_NS),
            "max_bytes": "1048576",
        },
    }
    assert json.loads(response.value) == expected
    second = _paid_policy_block(app, [14], height=2, seconds=10 * 86400 + 9)
    # FinalizeBlock has exposed an app hash but has not committed its policy.
    assert json.loads(_evidence_query(app).value) == expected
    asyncio.run(commit(app))
    committed = _evidence_query(app, height=2)
    body = json.loads(committed.value)
    assert committed.height == 2
    assert body["height"] == "2"
    assert body["app_hash"] == second.app_hash.hex()
    assert body["last_block_nanos"] == str((10 * 86400 + 9) * 1_000_000_000)
    assert body["evidence"]["max_age_duration"] == str(8 * DAY_NS)
    assert _evidence_query(app, height=1).code == 1


def test_evidence_query_never_reads_pending_or_cached_policy_values(app):
    import json

    _paid_policy_block(app, [])
    asyncio.run(commit(app))
    committed = _evidence_query(app)
    pending = EvidencePolicy.seed(106750, nanos=999 * DAY_NS).to_state()
    app.client.raw_driver.set(EVIDENCE_POLICY_KEY, pending)
    app.client.raw_driver.set(
        "__latest_block", {"height": 999, "hash": "ff" * 32, "nanos": 999 * DAY_NS}
    )
    assert app.client.raw_driver.get(EVIDENCE_POLICY_KEY) == pending
    current = _evidence_query(app)
    assert current.code == 0, current.log
    assert current.height == 1
    assert json.loads(current.value) == json.loads(committed.value)


def test_evidence_query_uses_canonical_strings_for_large_durations_and_future_nanoseconds(app):
    import json

    from google.protobuf.timestamp_pb2 import Timestamp

    future = Timestamp()
    future.FromJsonString("2500-01-01T00:00:00.123456789Z")
    policy = EvidencePolicy.seed(106750, nanos=future.ToNanoseconds())
    driver = app.client.raw_driver
    driver.set(EVIDENCE_POLICY_KEY, policy.to_state())
    driver.commit()
    app.state_root_cache.rebuild_from_driver(driver)
    from xian.utils.block import stage_latest_block

    root = app.state_root_cache.root_hash
    stage_latest_block(driver, block_hash=root, height=7, nanos=future.ToNanoseconds())
    driver.commit()
    response = _evidence_query(app, height=7)
    assert response.code == 0, response.log
    body = json.loads(response.value)
    assert body["last_block_nanos"] == "16725225600123456789"
    assert body["evidence"]["max_age_duration"] == str(106751 * DAY_NS)
    assert body["app_hash"] == root.hex()
    assert b"__big_int__" not in response.value


def test_evidence_query_is_unavailable_before_the_first_committed_block(app):
    response = _evidence_query(app)
    assert response.code == 1
    assert "committed" in response.log.lower()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "record",
        "anchor",
        "hash",
        "marker_type",
        "height_type",
        "uppercase_hash",
        "hash_type",
        "extra_field",
        "huge_height",
        "string_time",
    ],
)
def test_evidence_query_rejects_missing_and_corrupt_committed_data(app, corruption):
    _paid_policy_block(app, [])
    asyncio.run(commit(app))
    driver = app.client.raw_driver
    if corruption == "missing":
        driver.set(EVIDENCE_POLICY_KEY, None)
    elif corruption in ["record", "anchor"]:
        policy = driver.value_from_disk(EVIDENCE_POLICY_KEY)
        policy["version"] += 1 if corruption == "record" else 0
        if corruption == "anchor":
            policy["growth_start_nanos"] = driver.value_from_disk("__latest_block")["nanos"] + 1
        driver.set(EVIDENCE_POLICY_KEY, policy)
    else:
        marker = driver.value_from_disk("__latest_block")
        if corruption == "hash":
            marker["hash"] = "invalid"
        elif corruption == "marker_type":
            marker["nanos"] = True
        elif corruption == "height_type":
            marker["height"] = True
        elif corruption == "uppercase_hash":
            marker["hash"] = "AB" * 32
        elif corruption == "hash_type":
            marker["hash"] = 11
        elif corruption == "extra_field":
            marker["extra"] = 1
        elif corruption == "huge_height":
            marker["height"] = 2**63
        else:
            marker["nanos"] = str(marker["nanos"])
        driver.set("__latest_block", marker)
    driver.commit()
    response = _evidence_query(app)
    assert response.code == 1
    assert response.log


def test_evidence_query_reads_a_bounded_number_of_committed_keys(app):
    driver = app.client.raw_driver
    for index in range(2000):
        driver.set(f"con_unrelated.values:{index}", index)
    _paid_policy_block(app, [])
    asyncio.run(commit(app))
    with (
        patch.object(driver, "iter_items", side_effect=AssertionError("full scan")),
        patch.object(driver, "items", side_effect=AssertionError("full scan")),
        patch.object(driver, "value_from_disk", wraps=driver.value_from_disk) as reads,
        patch.object(app.tx_processor, "process_system_tx", side_effect=AssertionError("VM call")),
    ):
        response = _evidence_query(app)
    assert response.code == 0, response.log
    assert reads.call_count <= 3
    assert {call.args[0] for call in reads.call_args_list} <= {
        EVIDENCE_POLICY_KEY,
        "__latest_block",
    }


def test_evidence_query_preserves_negative_subsecond_timestamp_and_refuses_path_arguments(app):
    import json

    from xian.utils.block import stage_latest_block

    driver = app.client.raw_driver
    driver.set(EVIDENCE_POLICY_KEY, EvidencePolicy.seed(0, nanos=-1).to_state())
    driver.commit()
    root = app.state_root_cache.rebuild_from_driver(driver)
    stage_latest_block(driver, block_hash=root, height=1, nanos=-1)
    driver.commit()
    response = _evidence_query(app, height=1)
    assert response.code == 0, response.log
    assert json.loads(response.value)["last_block_nanos"] == "-1"
    unexpected = _evidence_query(app, path="/evidence_policy/unused")
    assert unexpected.code == 1
    assert "does not accept" in unexpected.log
