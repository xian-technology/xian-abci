"""Authenticated schedules preserve cadence without spending unauthorised time."""

import pytest
from contracting.storage.driver import Driver

from xian.evidence_policy import DAY_NS, EVIDENCE_POLICY_KEY, EvidencePolicy, stage_evidence_policy
from xian.execution_policy import current_execution_policy
from xian.state_root import compute_driver_state_root


@pytest.mark.parametrize("pending", [False, True])
def test_empty_blocks_do_not_rewrite_authenticated_schedule_or_app_root(tmp_path, pending):
    policy = EvidencePolicy.seed(7, nanos=10 * DAY_NS)
    if pending:
        policy = policy.advance(nanos=10 * DAY_NS, observed_days=[14])
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set(EVIDENCE_POLICY_KEY, policy.to_state())
        driver.commit()
        before = compute_driver_state_root(driver)
        params = stage_evidence_policy(
            driver, nanos=11 * DAY_NS, processed_entries=[], applied_patches=[]
        )
        assert compute_driver_state_root(driver) == before
        assert driver.get(EVIDENCE_POLICY_KEY) == policy.to_state()
        if pending:
            assert params.evidence.max_age_duration.seconds == 9 * 86400


def test_new_target_materializes_only_the_old_authorized_ramp_after_long_gap():
    old = EvidencePolicy.seed(7, nanos=0)
    started = old.advance(nanos=100 * DAY_NS, observed_days=[14])
    assert started.comet_json()["max_age_duration"] == str(8 * DAY_NS)
    extended = started.advance(nanos=200 * DAY_NS, observed_days=[30])
    assert extended.comet_json()["max_age_duration"] == str(15 * DAY_NS)


def test_recovery_cannot_credit_a_forged_export_checkpoint_time():
    from xian.recovery_genesis import build_recovery_genesis
    from xian.state_root import compute_exported_state_root

    validator = "11" * 32
    policy = EvidencePolicy.seed(7, nanos=0).advance(nanos=0, observed_days=[14])
    export = {
        "hash": "",
        "number": 100,
        "nanos": DAY_NS,
        "genesis": [
            {"key": EVIDENCE_POLICY_KEY, "value": policy.to_state()},
            {"key": "__execution_policy", "value": current_execution_policy()},
            {"key": "validators.active_validators", "value": [validator]},
            {"key": "validators.__consensus__", "value": {validator: 10}},
        ],
    }
    export["hash"] = compute_exported_state_root(export).hex()
    checkpoint = {
        "schema_version": 1,
        "height": "100",
        "app_hash": export["hash"],
        "last_block_nanos": str(DAY_NS),
    }
    export["nanos"] = 100 * DAY_NS  # Root is unchanged; checkpoint time is not state.
    with pytest.raises(ValueError, match="independently trusted checkpoint"):
        build_recovery_genesis(
            export,
            [validator],
            chain_id="c",
            genesis_time="1971-01-01T00:00:00Z",
            trusted_checkpoint=checkpoint,
        )


@pytest.mark.parametrize("raise_elapsed", [7, DAY_NS])
def test_nanosecond_cutoff_is_preserved_when_an_unfinished_target_rises(raise_elapsed):
    start = 100 * DAY_NS + 123456789
    first = EvidencePolicy.seed(7, nanos=start).advance(nanos=start, observed_days=[14])
    raised = first.advance(nanos=start + raise_elapsed, observed_days=[30])
    before_duration = first.effective_duration(start + raise_elapsed)
    assert raised.base_max_age_duration == before_duration
    assert raised.growth_start_nanos == start + raise_elapsed
    for elapsed in (0, 1, 999, DAY_NS):
        point = start + raise_elapsed + elapsed
        assert point - raised.effective_duration(point) == start - 8 * DAY_NS
    assert raised.advance(nanos=start + raise_elapsed + 1, observed_days=[0]) == raised


@pytest.mark.parametrize("point", [0, 1, 86400000000000])
def test_different_local_markers_cannot_suppress_the_same_consensus_update(tmp_path, point):
    policy = EvidencePolicy.seed(7, nanos=0).advance(nanos=0, observed_days=[14])
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set(EVIDENCE_POLICY_KEY, policy.to_state())
        driver.set("__latest_block", {"height": 1, "hash": "a" * 64, "nanos": point})
        driver.commit()
        response = stage_evidence_policy(
            driver, nanos=2 * DAY_NS, processed_entries=[], applied_patches=[]
        )
        assert response == policy.consensus_params(nanos=2 * DAY_NS)


def test_schema1_is_not_silently_interpreted_as_a_schedule():
    legacy = {
        "version": 1,
        "max_age_num_blocks": 100000,
        "max_age_duration": 8 * DAY_NS,
        "max_bytes": 1048576,
        "target_max_age_duration": 15 * DAY_NS,
        "last_block_nanos": 0,
    }
    with pytest.raises(ValueError, match="schema version 2"):
        EvidencePolicy.from_state(legacy)


@pytest.mark.parametrize("value", [True, 1.0, "1", None, -1])
def test_effective_schedule_rejects_noncanonical_or_before_anchor_times(value):
    with pytest.raises(ValueError):
        EvidencePolicy.seed(7, nanos=0).effective_duration(value)


def test_effective_schedule_respects_duration_int64_and_full_timestamp_domain():
    from xian.evidence_policy import INT64_MAX, TIMESTAMP_MAX_NS, comet_time_nanos

    point = comet_time_nanos("2500-01-01T00:00:00.123456789Z")
    policy = EvidencePolicy.seed(
        0,
        nanos=point,
        params={"max_age_num_blocks": 1, "max_age_duration": INT64_MAX - 1, "max_bytes": 0},
    ).advance(nanos=point, observed_days=[106750])
    # A preconfigured duration larger than the required target is already final.
    assert policy.effective_duration(TIMESTAMP_MAX_NS) == INT64_MAX - 1
    assert policy.consensus_params(nanos=point).evidence.max_bytes == 0
    with pytest.raises(ValueError):
        policy.effective_duration(TIMESTAMP_MAX_NS + 1)


def _pending_export_fixture():
    from xian.state_root import compute_exported_state_root

    validator = "11" * 32
    policy = EvidencePolicy.seed(7, nanos=10 * DAY_NS + 123456789).advance(
        nanos=10 * DAY_NS + 123456789, observed_days=[14]
    )
    point = 11 * DAY_NS + 123456790
    export = {
        "hash": "",
        "number": 100,
        "nanos": point,
        "genesis": [
            {"key": EVIDENCE_POLICY_KEY, "value": policy.to_state()},
            {"key": "__execution_policy", "value": current_execution_policy()},
            {"key": "validators.active_validators", "value": [validator]},
            {"key": "validators.__consensus__", "value": {validator: 10}},
        ],
    }
    export["hash"] = compute_exported_state_root(export).hex()
    checkpoint = {
        "schema_version": 1,
        "height": "100",
        "app_hash": export["hash"],
        "last_block_nanos": str(point),
    }
    return export, checkpoint, policy, validator


def test_offline_roundtrip_and_verified_recovery_materialize_only_the_checkpoint(tmp_path):
    from xian_runtime_types.encoding import encode_wire

    from xian.evidence_policy import comet_time_nanos
    from xian.recovery_genesis import build_recovery_genesis
    from xian.state_export import import_state, load_exported_state, prepare_state_import

    export, checkpoint, policy, validator = _pending_export_fixture()
    path = tmp_path / "export.json"
    path.write_text(encode_wire(export, vm_profile="xian_vm_v2"))
    decoded = load_exported_state(path)
    prepared = prepare_state_import(decoded)
    assert prepared.nanos == 11 * DAY_NS + 123456790
    import_state(exported_state=decoded, storage_home=tmp_path / "import")
    with Driver(storage_home=tmp_path / "import", vm_profile="xian_vm_v2") as driver:
        assert driver.get(EVIDENCE_POLICY_KEY) == policy.to_state()
        assert compute_driver_state_root(driver).hex() == checkpoint["app_hash"]
    recovered = build_recovery_genesis(
        decoded,
        [validator],
        chain_id="c",
        genesis_time="1971-01-01T00:00:00.123456791Z",
        trusted_checkpoint=checkpoint,
    )
    record = next(
        e["value"] for e in recovered["abci_genesis"]["genesis"] if e["key"] == EVIDENCE_POLICY_KEY
    )
    assert record["base_max_age_duration"] == 9 * DAY_NS + 1
    assert record["target_max_age_duration"] == 15 * DAY_NS
    assert record["growth_start_nanos"] == comet_time_nanos("1971-01-01T00:00:00.123456791Z")
    assert recovered["consensus_params"]["evidence"]["max_age_duration"] == str(9 * DAY_NS + 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("height", "101"),
        ("app_hash", "a" * 64),
        ("last_block_nanos", "1"),
        ("height", True),
        ("schema_version", True),
    ],
)
def test_recovery_requires_an_exact_independent_canonical_checkpoint(field, value):
    from xian.recovery_genesis import build_recovery_genesis

    export, checkpoint, _, validator = _pending_export_fixture()
    with pytest.raises(ValueError):
        build_recovery_genesis(
            export,
            [validator],
            chain_id="c",
            genesis_time="1971-01-01T00:00:00Z",
            trusted_checkpoint={**checkpoint, field: value},
        )
    with pytest.raises(ValueError, match="trusted checkpoint"):
        build_recovery_genesis(
            export,
            [validator],
            chain_id="c",
            genesis_time="1971-01-01T00:00:00Z",
            trusted_checkpoint=export,
        )


@pytest.mark.parametrize("replacement", [None, True, "950400123456790"])
def test_positive_height_export_requires_explicit_exact_checkpoint_nanos(replacement):
    from xian.state_export import prepare_state_import

    export, *_ = _pending_export_fixture()
    if replacement is None:
        del export["nanos"]
    else:
        export["nanos"] = replacement
    with pytest.raises(ValueError, match="checkpoint nanos"):
        prepare_state_import(export)


def test_default_outer_genesis_uses_tagged_checkpoint_time_after_json_roundtrip(tmp_path):
    import json

    from xian_runtime_types.encoding import encode_wire

    from xian.evidence_policy import comet_time_nanos
    from xian.genesis_builder import build_cometbft_genesis, finalize_genesis_block

    point = comet_time_nanos("2500-01-01T00:00:00.123456789Z")
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set("validators.config:unbonding_period_days", 14)
        prepared = finalize_genesis_block(driver, founder_private_key=None, nanos=point)
    serialized = json.loads(encode_wire(prepared, vm_profile="xian_vm_v2"))
    assert isinstance(serialized["nanos"], dict)
    genesis = build_cometbft_genesis(chain_id="c", abci_genesis=serialized)
    assert genesis["genesis_time"] == "2500-01-01T00:00:00.123456789Z"
    assert genesis["consensus_params"]["evidence"]["max_age_duration"] == str(15 * DAY_NS)


@pytest.mark.parametrize("number", [None, 0, "0"])
def test_epoch_genesis_without_checkpoint_metadata_uses_authenticated_anchor(number):
    from xian.genesis_builder import build_cometbft_genesis
    from xian.state_root import compute_exported_state_root

    policy = EvidencePolicy.seed(7, nanos=123)
    state = {"genesis": [{"key": EVIDENCE_POLICY_KEY, "value": policy.to_state()}]}
    if number is not None:
        state["number"] = number
    state["hash"] = compute_exported_state_root(state).hex()
    genesis = build_cometbft_genesis(chain_id="c", abci_genesis=state)
    assert genesis["genesis_time"] == "1970-01-01T00:00:00.000000123Z"
    assert genesis["consensus_params"]["evidence"] == policy.comet_json()
