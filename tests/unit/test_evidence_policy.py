"""Evidence expiry must cover the chain's withdrawal obligations."""

import json

import pytest
from contracting.storage.driver import Driver

from xian.genesis_builder import build_cometbft_genesis, finalize_genesis_block
from xian.state_root import compute_exported_state_root
from xian.utils.state_patches import StatePatchManager

DAY_NS = 86_400_000_000_000
POLICY_KEY = "__evidence_policy"
UNBONDING_KEY = "validators.config:unbonding_period_days"


def test_fresh_genesis_covers_seeded_unbonding_and_commits_the_policy(tmp_path):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(UNBONDING_KEY, 14)
        abci = finalize_genesis_block(
            driver, founder_private_key=None, nanos=1_794_787_200_000_000_000
        )
    state = {entry["key"]: entry["value"] for entry in abci["genesis"]}
    assert state[POLICY_KEY]["base_max_age_duration"] == 15 * DAY_NS
    assert bytes.fromhex(abci["hash"]) == compute_exported_state_root(abci)
    genesis = build_cometbft_genesis(
        chain_id="retention-test", abci_genesis=abci, genesis_time="2026-11-16T00:00:00Z"
    )
    assert genesis["consensus_params"]["evidence"] == {
        "max_age_num_blocks": "100000",
        "max_age_duration": str(15 * DAY_NS),
        "max_bytes": "1048576",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        (POLICY_KEY, {}),
        (UNBONDING_KEY, 106751),
        (UNBONDING_KEY, True),
        (UNBONDING_KEY, 14.0),
        (UNBONDING_KEY, "14"),
        (UNBONDING_KEY, -1),
        (UNBONDING_KEY, None),
    ],
)
def test_unsupported_policy_patches_are_rejected_before_activation(tmp_path, key, value):
    payload = {
        "version": 1,
        "patch_id": "invalid-policy",
        "activation_height": 12,
        "changes": [{"key": key, "value": value}],
    }
    (tmp_path / "policy.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="evidence|unbonding_period_days|fractional JSON"):
        StatePatchManager(None, chain_id="retention-test").load_patches(tmp_path)


def test_duration_growth_never_reopens_the_expired_history_cutoff():
    from xian.evidence_policy import EvidencePolicy

    old = EvidencePolicy.seed(1, nanos=100 * DAY_NS)
    stable = old.advance(nanos=130 * DAY_NS, observed_days=[1])
    assert stable == old
    grown = stable.advance(nanos=130 * DAY_NS + 7, observed_days=[14])
    assert grown.base_max_age_duration == old.base_max_age_duration
    assert grown.target_max_age_duration == 15 * DAY_NS
    assert grown.effective_duration(131 * DAY_NS + 7) == 3 * DAY_NS
    assert (
        131 * DAY_NS + 7 - grown.effective_duration(131 * DAY_NS + 7)
        == 130 * DAY_NS + 7 - grown.base_max_age_duration
    )
    assert grown.advance(nanos=131 * DAY_NS + 7, observed_days=[0]) == grown
    assert grown.effective_duration(160 * DAY_NS) == 15 * DAY_NS


def test_full_evidence_update_preserves_custom_limits_and_omits_other_groups():
    from xian.evidence_policy import EvidencePolicy

    policy = EvidencePolicy.seed(
        14,
        params={"max_age_num_blocks": 12345, "max_age_duration": 15 * DAY_NS + 7, "max_bytes": 42},
    )
    update = policy.consensus_params()
    assert [field.name for field, _ in update.ListFields()] == ["evidence"]
    assert update.evidence.max_age_num_blocks == 12345
    assert update.evidence.max_bytes == 42
    assert update.evidence.max_age_duration.seconds == 15 * 86400
    assert update.evidence.max_age_duration.nanos == 7


@pytest.mark.parametrize("days", [False, True, "14", -1, 106751, 2**63])
def test_unsupported_days_fail_at_genesis_not_first_block(tmp_path, days):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(UNBONDING_KEY, days)
        with pytest.raises(ValueError, match="unbonding_period_days"):
            finalize_genesis_block(driver, founder_private_key=None)


@pytest.mark.parametrize("days", [0, 14, 106750])
def test_supported_duration_bounds_fit_comets_signed_nanoseconds(days):
    from xian.evidence_policy import INT64_MAX, MAX_UNBONDING_PERIOD_DAYS, EvidencePolicy

    assert MAX_UNBONDING_PERIOD_DAYS == 106750
    policy = EvidencePolicy.seed(days)
    assert 0 < policy.base_max_age_duration <= INT64_MAX
    assert policy.base_max_age_duration >= (days + 1) * DAY_NS


def test_export_refuses_a_noncanonical_highwater_even_with_a_matching_root(tmp_path):
    from xian.state_export import prepare_state_import

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(UNBONDING_KEY, 14)
        exported = finalize_genesis_block(driver, founder_private_key=None)
    policy = next(e["value"] for e in exported["genesis"] if e["key"] == POLICY_KEY)
    policy["base_max_age_duration"] = True
    exported["hash"] = compute_exported_state_root(exported).hex()
    with pytest.raises(ValueError, match="evidence"):
        prepare_state_import(exported)


def test_recovery_reanchors_time_without_shrinking_the_effective_window(tmp_path):
    from xian.evidence_policy import EvidencePolicy
    from xian.recovery_genesis import build_recovery_genesis

    validator = "11" * 32
    policy = EvidencePolicy.seed(14, nanos=100 * DAY_NS)
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(UNBONDING_KEY, 7)
        driver.set(POLICY_KEY, policy.to_state())
        driver.set("validators.active_validators", [validator])
        driver.set("validators.__consensus__", {validator: 10})
        exported = finalize_genesis_block(driver, founder_private_key=None, number=100)
    exported["nanos"] = 100 * DAY_NS
    recovered = build_recovery_genesis(
        exported,
        [validator],
        trusted_checkpoint={
            "schema_version": 1,
            "height": "100",
            "app_hash": exported["hash"],
            "last_block_nanos": str(exported["nanos"]),
        },
        chain_id="retention-test",
        genesis_time="1970-07-20T00:00:00Z",
    )
    recovered_policy = next(
        e["value"] for e in recovered["abci_genesis"]["genesis"] if e["key"] == POLICY_KEY
    )
    assert recovered_policy["base_max_age_duration"] == 15 * DAY_NS
    assert recovered_policy["target_max_age_duration"] == 15 * DAY_NS
    assert recovered_policy["growth_start_nanos"] == 200 * DAY_NS


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1),
        ("base_max_age_duration", True),
        ("base_max_age_duration", 0),
        ("base_max_age_duration", 2**63),
        ("max_age_num_blocks", 0),
        ("max_age_num_blocks", "100000"),
        ("max_bytes", -1),
        ("target_max_age_duration", 0),
        ("growth_start_nanos", True),
        ("extra", 1),
    ],
)
def test_highwater_record_requires_exact_canonical_fields_and_types(field, value):
    from xian.evidence_policy import EvidencePolicy

    state = EvidencePolicy.seed(14).to_state()
    state[field] = value
    with pytest.raises(ValueError, match="evidence"):
        EvidencePolicy.from_state(state)


def test_evidence_highwater_is_actually_part_of_the_consensus_hash(tmp_path):
    from xian.evidence_policy import EvidencePolicy
    from xian.state_root import StateRootCache

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(POLICY_KEY, EvidencePolicy.seed(7).to_state())
        driver.commit()
        before = StateRootCache.from_driver(driver).root_hash
        driver.set(POLICY_KEY, EvidencePolicy.seed(14).to_state())
        driver.commit()
        assert StateRootCache.from_driver(driver).root_hash != before


def test_epoch_zero_and_post2262_timestamp_anchors_are_valid():
    from xian.evidence_policy import EvidencePolicy, comet_time_nanos

    epoch = EvidencePolicy.seed(7, nanos=0)
    grown = epoch.advance(nanos=DAY_NS, observed_days=[14])
    assert grown.base_max_age_duration == 8 * DAY_NS
    assert grown.effective_duration(2 * DAY_NS) == 9 * DAY_NS
    future = comet_time_nanos("2500-01-01T00:00:00.123456789Z")
    assert future > 2**63
    policy = EvidencePolicy.seed(106750, nanos=future)
    assert policy.advance(nanos=future + 1, observed_days=[]) == policy
    with pytest.raises(ValueError, match="block nanos"):
        policy.advance(nanos=future - 1, observed_days=[])


def test_nine_digit_genesis_timestamp_and_recovery_anchor_do_not_round_down(tmp_path):
    from xian.evidence_policy import comet_time_nanos
    from xian.genesis_builder import normalize_genesis_time
    from xian.recovery_genesis import build_recovery_genesis

    exact = "2026-11-16T00:00:00.123456789Z"
    assert normalize_genesis_time(exact) == exact
    nanos = comet_time_nanos(exact)
    validator = "11" * 32
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(UNBONDING_KEY, 14)
        driver.set("validators.active_validators", [validator])
        driver.set("validators.__consensus__", {validator: 10})
        exported = finalize_genesis_block(driver, founder_private_key=None, nanos=nanos, number=100)
    exported["nanos"] = nanos
    recovered = build_recovery_genesis(
        exported,
        [validator],
        trusted_checkpoint={
            "schema_version": 1,
            "height": "100",
            "app_hash": exported["hash"],
            "last_block_nanos": str(exported["nanos"]),
        },
        chain_id="c",
        genesis_time="2026-11-16T00:00:00.123456790Z",
    )
    record = next(
        e["value"] for e in recovered["abci_genesis"]["genesis"] if e["key"] == POLICY_KEY
    )
    assert record["growth_start_nanos"] == nanos + 1
    assert recovered["genesis_time"].endswith("123456790Z")


def test_full_parameter_domain_and_canonical_target_relationship():
    from xian.evidence_policy import DEFAULT_EVIDENCE_PARAMS, INT64_MAX, EvidencePolicy

    policy = EvidencePolicy.seed(
        0, params={"max_age_num_blocks": INT64_MAX, "max_age_duration": INT64_MAX, "max_bytes": 0}
    )
    assert policy.base_max_age_duration == INT64_MAX
    assert policy.max_age_num_blocks == INT64_MAX
    assert policy.consensus_params().evidence.max_bytes == 0
    state = EvidencePolicy.seed(14).to_state()
    state["target_max_age_duration"] -= 1
    with pytest.raises(ValueError, match="target"):
        EvidencePolicy.from_state(state)
    with pytest.raises(ValueError, match="full limits triple"):
        EvidencePolicy.seed(14, params={})
    for field in DEFAULT_EVIDENCE_PARAMS:
        params = dict(DEFAULT_EVIDENCE_PARAMS)
        params[field] = False
        with pytest.raises(ValueError, match="evidence"):
            EvidencePolicy.seed(14, params=params)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "uncovered", "wrong_time", "wrong_params", "wrong_export_time"],
)
def test_external_genesis_and_export_must_agree_with_the_protected_record(mutation):
    from xian.evidence_policy import EvidencePolicy, validate_genesis_evidence_policy

    policy = EvidencePolicy.seed(7, nanos=0)
    entries = [{"key": POLICY_KEY, "value": policy.to_state()}, {"key": UNBONDING_KEY, "value": 7}]
    genesis = {
        "genesis_time": "1970-01-01T00:00:00Z",
        "consensus_params": {"evidence": policy.comet_json()},
        "abci_genesis": {"genesis": entries},
    }
    if mutation == "missing":
        entries.pop(0)
    elif mutation == "duplicate":
        entries.append(dict(entries[0]))
    elif mutation == "uncovered":
        entries[1]["value"] = 14
    elif mutation == "wrong_time":
        genesis["genesis_time"] = "1970-01-01T00:00:00.000000001Z"
    elif mutation == "wrong_params":
        genesis["consensus_params"]["evidence"]["max_age_num_blocks"] = "99999"
    else:
        genesis["abci_genesis"]["nanos"] = 1
    with pytest.raises(ValueError, match="evidence"):
        validate_genesis_evidence_policy(genesis)


def test_default_anchor_is_literal_epoch_zero_and_zero_duration_is_invalid_input():
    from xian.evidence_policy import DEFAULT_EVIDENCE_PARAMS, EvidencePolicy

    assert EvidencePolicy.seed(7).growth_start_nanos == 0
    for field in ["max_age_num_blocks", "max_age_duration"]:
        params = dict(DEFAULT_EVIDENCE_PARAMS)
        params[field] = 0
        with pytest.raises(ValueError, match="evidence"):
            EvidencePolicy.seed(14, params=params)


def test_init_time_is_explicit_even_at_epoch_zero_and_unrelated_groups_are_optional():
    from google.protobuf.timestamp_pb2 import Timestamp

    from tendermint.abci.types_pb2 import RequestInitChain
    from tendermint.types.params_pb2 import BlockParams, ConsensusParams
    from xian.evidence_policy import EvidencePolicy, validate_init_evidence_request

    policy = EvidencePolicy.seed(7, nanos=0)
    for missing in [None, RequestInitChain()]:
        with pytest.raises(ValueError, match="InitChain time"):
            validate_init_evidence_request(missing, policy)
    request = RequestInitChain(
        time=Timestamp(seconds=0),
        consensus_params=ConsensusParams(block=BlockParams(max_bytes=1000000)),
    )
    validate_init_evidence_request(request, policy)


def test_updating_an_existing_genesis_refuses_wrong_anchor_without_touching_the_file(tmp_path):
    from xian.evidence_policy import EvidencePolicy
    from xian.genesis_builder import update_cometbft_genesis

    path = tmp_path / "genesis.json"
    original = json.dumps({"genesis_time": "1970-01-01T00:00:00Z", "chain_id": "c"})
    path.write_text(original)
    replacement = {
        "hash": "abc",
        "number": "0",
        "genesis": [{"key": POLICY_KEY, "value": EvidencePolicy.seed(7, nanos=1).to_state()}],
    }
    with pytest.raises(ValueError, match="time anchor|checkpoint|block nanos"):
        update_cometbft_genesis(path, abci_genesis=replacement)
    assert path.read_text() == original


def test_staged_genesis_refuses_an_existing_record_that_loses_a_longer_obligation(tmp_path):
    from xian.evidence_policy import EvidencePolicy

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        driver.set(POLICY_KEY, EvidencePolicy.seed(7).to_state())
        driver.set(UNBONDING_KEY, 14)
        with pytest.raises(ValueError, match="evidence.*cover"):
            finalize_genesis_block(driver, founder_private_key=None)
        assert driver.value_from_disk(UNBONDING_KEY) is None


def test_minimal_staged_genesis_has_literal_epoch_and_preserves_a_sufficient_custom_window(
    tmp_path,
):
    from xian.evidence_policy import EvidencePolicy

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        initial = finalize_genesis_block(driver, founder_private_key=None)
        first = next(e["value"] for e in initial["genesis"] if e["key"] == POLICY_KEY)
        assert first["growth_start_nanos"] == 0
        assert first["base_max_age_duration"] == 2 * DAY_NS
        custom = EvidencePolicy.seed(
            0, params={"max_age_num_blocks": 123, "max_age_duration": DAY_NS, "max_bytes": 42}
        )
        driver.set(POLICY_KEY, custom.to_state())
        repeated = finalize_genesis_block(driver, founder_private_key=None)
        record = next(e["value"] for e in repeated["genesis"] if e["key"] == POLICY_KEY)
        assert record == custom.to_state()


def test_explicit_staged_time_cannot_be_silently_ignored_by_an_existing_record(tmp_path):
    from xian.evidence_policy import EvidencePolicy

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path / "state") as driver:
        policy = EvidencePolicy.seed(7, nanos=123)
        driver.set(POLICY_KEY, policy.to_state())
        driver.commit()
        driver.set("con_layered.value", "pending")
        with pytest.raises(ValueError, match="evidence.*anchor|evidence.*time"):
            finalize_genesis_block(driver, founder_private_key=None, nanos=124)
        assert driver.value_from_disk("con_layered.value") is None
        assert driver.value_from_disk(POLICY_KEY) == policy.to_state()
        finalize_genesis_block(driver, founder_private_key=None, nanos=123)
        assert driver.value_from_disk("con_layered.value") == "pending"
        assert driver.value_from_disk(POLICY_KEY) == policy.to_state()
