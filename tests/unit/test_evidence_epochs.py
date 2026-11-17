"""A fresh stale-time fault must outlive its later successor challenge hold."""

from xian.evidence_policy import DAY_NS, EvidencePolicy


def _expired(height, now, evidence_height, evidence_time, limits):
    # Stock Comet requires BOTH strict ages; this oracle does not call policy math.
    return height - evidence_height > int(
        limits["max_age_num_blocks"]
    ) and now - evidence_time > int(limits["max_age_duration"])


def test_stale_parent_fault_is_retained_when_unbond_request_is_much_later():
    policy = EvidencePolicy.seed(14, nanos=0)
    after_jump = policy.advance(nanos=100 * DAY_NS, observed_days=[], height=2)
    # Fault H1/T0 was fresh in real time; the request may only arrive at H50000.
    limits = after_jump.comet_json(nanos=101 * DAY_NS, height=100002)
    assert not _expired(100002, 101 * DAY_NS, 1, 1, limits)


def test_large_time_jump_records_one_security_transition_then_settles():
    policy = EvidencePolicy.seed(14, nanos=0)
    after_jump = policy.advance(nanos=100 * DAY_NS, observed_days=[], height=2)
    assert after_jump.to_state() != policy.to_state()
    assert (
        after_jump.advance(nanos=100 * DAY_NS + 1, observed_days=[], height=3).to_state()
        == after_jump.to_state()
    )


def test_next_rotation_retires_only_the_older_floor_and_never_revives_it():
    policy = EvidencePolicy.seed(14, nanos=123)
    jumped = policy.advance(nanos=100 * DAY_NS + 123, height=2, observed_days=[])
    assert jumped.previous_security_checkpoint_height == 0  # Zero is not None.
    before = jumped.advance(nanos=115 * DAY_NS + 122, height=100002, observed_days=[])
    assert before == jumped
    retired = before.advance(nanos=115 * DAY_NS + 123, height=100003, observed_days=[])
    assert retired.previous_security_checkpoint_height == 2
    assert _expired(
        100003,
        115 * DAY_NS + 123,
        1,
        123,
        retired.comet_json(nanos=115 * DAY_NS + 123, height=100003),
    )
    raised = retired.advance(nanos=115 * DAY_NS + 124, height=100004, observed_days=[30])
    assert raised.previous_security_checkpoint_height == 2
    assert _expired(
        100004,
        115 * DAY_NS + 124,
        1,
        123,
        raised.comet_json(nanos=115 * DAY_NS + 124, height=100004),
    )


def test_target_increase_extends_live_epochs_before_rotation_without_idle_credit():
    policy = EvidencePolicy.seed(
        7, nanos=123, params={"max_age_num_blocks": 1, "max_age_duration": DAY_NS, "max_bytes": 9}
    )
    # Eight days would rotate the old epoch. The increased target delays it.
    raised = policy.advance(nanos=10 * DAY_NS + 123, height=10, observed_days=[30])
    assert raised.security_checkpoint_height == 0
    assert raised.previous_security_checkpoint_height is None
    assert raised.base_max_age_duration == 8 * DAY_NS
    rotated = raised.advance(nanos=31 * DAY_NS + 123, height=100, observed_days=[])
    assert rotated.previous_security_checkpoint_height == 0
    assert rotated.security_checkpoint_height == 100
    # The duration ramp and security epoch clocks have no fixed ordering.
    assert rotated.growth_start_nanos < rotated.security_checkpoint_nanos
    raised_again = rotated.advance(nanos=32 * DAY_NS + 123, height=101, observed_days=[60])
    assert raised_again.growth_start_nanos > raised_again.security_checkpoint_nanos


def test_both_expiry_cutoffs_are_monotonic_across_gaps_raises_and_reductions():
    policy = EvidencePolicy.seed(
        1, nanos=123, params={"max_age_num_blocks": 1, "max_age_duration": DAY_NS, "max_bytes": 0}
    )
    previous = None
    for height, days, observed in [
        (1, 0, []),
        (2, 100, []),
        (50000, 101, [14]),
        (100002, 102, [7]),
        (200000, 116, []),
        (300000, 150, []),
        (400000, 151, [60]),
        (500000, 300, []),
    ]:
        point = days * DAY_NS + 123
        policy = policy.advance(nanos=point, height=height, observed_days=observed)
        limits = policy.effective_limits(nanos=point, height=height)
        cutoffs = (height - limits["max_age_num_blocks"], point - limits["max_age_duration"])
        if previous is not None:
            assert all(now >= old for now, old in zip(cutoffs, previous, strict=True))
        previous = cutoffs


def test_checkpoint_domains_and_large_height_duration_are_independent():
    from dataclasses import replace

    import pytest

    from xian.evidence_policy import INT64_MAX, comet_time_nanos

    point = comet_time_nanos("2500-01-01T00:00:00.123456789Z")
    policy = EvidencePolicy.seed(
        106750, nanos=point, params={"max_age_num_blocks": 1, "max_age_duration": 1, "max_bytes": 0}
    )
    assert policy.effective_limits(nanos=point, height=INT64_MAX)["max_age_num_blocks"] == INT64_MAX
    for field, value in [
        ("previous_security_checkpoint_height", True),
        ("previous_security_checkpoint_height", 1),
        ("security_checkpoint_height", -1),
        ("security_checkpoint_height", INT64_MAX + 1),
        ("security_checkpoint_nanos", True),
    ]:
        with pytest.raises(ValueError):
            replace(policy, **{field: value})
    for height, nanos in [(True, point), (INT64_MAX + 1, point), (0, point - 1)]:
        with pytest.raises(ValueError):
            policy.effective_limits(nanos=nanos, height=height)
    with pytest.raises(ValueError):
        replace(policy, security_checkpoint_height=2).effective_limits(nanos=point, height=1)


def test_serialized_large_checkpoint_height_builds_canonical_initial_height():
    import json

    from xian_runtime_types.encoding import encode_wire

    from xian.execution_policy import current_execution_policy
    from xian.genesis_builder import build_cometbft_genesis

    height = 2**53 + 1
    policy = EvidencePolicy.seed(7, nanos=0)
    export = {
        "number": height,
        "nanos": 0,
        "hash": "a" * 64,
        "genesis": [
            {"key": "__evidence_policy", "value": policy.to_state()},
            {"key": "__execution_policy", "value": current_execution_policy()},
        ],
    }
    serialized = json.loads(encode_wire(export, vm_profile="xian_vm_v2"))
    result = build_cometbft_genesis(chain_id="canonical", abci_genesis=serialized)
    assert result["initial_height"] == str(height)
    assert result["consensus_params"]["evidence"]["max_age_num_blocks"] == str(height)
