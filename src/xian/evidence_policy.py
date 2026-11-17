"""Committed evidence retention and its withdrawal-policy bounds.

CometBFT expires proof only after BOTH its block and time age exceed the
limits. A sparse pair of rooted security checkpoints protects fresh faults with stale
header time, without assuming a block rate or rewriting state every block. Duration growth must preserve the old
expiry cutoff so that previously pruned evidence never becomes valid again.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp
from xian_runtime_types.encoding import decode_wire_numbers

from tendermint.types.params_pb2 import ConsensusParams, EvidenceParams

EVIDENCE_POLICY_KEY = "__evidence_policy"
UNBONDING_PERIOD_KEY = "validators.config:unbonding_period_days"
DAY_NS = 86_400_000_000_000
INT64_MAX = (1 << 63) - 1
MAX_UNBONDING_PERIOD_DAYS = INT64_MAX // DAY_NS - 1
# Timestamp and Duration have different domains: valid future withdrawals can
# occur after the Unix-nanosecond int64 boundary in 2262.
TIMESTAMP_MIN_NS = -62_135_596_800 * 1_000_000_000
TIMESTAMP_MAX_NS = 253_402_300_799 * 1_000_000_000 + 999_999_999
DEFAULT_EVIDENCE_PARAMS = {
    "max_age_num_blocks": 100_000,
    "max_age_duration": 2 * DAY_NS,
    "max_bytes": 1_048_576,
}


def validated_unbonding_days(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_UNBONDING_PERIOD_DAYS:
        raise ValueError(
            f"unbonding_period_days must be an integer between 0 and {MAX_UNBONDING_PERIOD_DAYS}"
        )
    return value


def required_evidence_duration(days: object) -> int:
    # One whole day is a deterministic margin for consensus/update timing.
    return (validated_unbonding_days(days) + 1) * DAY_NS


def _integer(value: object, name: str, *, minimum: int = 1, maximum: int = INT64_MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"evidence policy {name} must be an integer in [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True)
class EvidencePolicy:
    """A rooted duration schedule and two sparse security checkpoints.

    The finalized consensus timestamp supplies evaluation time. Keeping the
    schedule immutable between policy changes avoids Comet proof-block loops;
    neither the schedule nor its security-critical anchor leaves the app root.
    """

    max_age_num_blocks: int
    base_max_age_duration: int
    max_bytes: int
    target_max_age_duration: int
    growth_start_nanos: int
    security_checkpoint_height: int
    security_checkpoint_nanos: int
    previous_security_checkpoint_height: int | None

    def __post_init__(self):
        _integer(self.max_age_num_blocks, "max_age_num_blocks")
        _integer(self.base_max_age_duration, "base_max_age_duration")
        _integer(self.max_bytes, "max_bytes", minimum=0)
        _integer(self.target_max_age_duration, "target_max_age_duration")
        _integer(
            self.growth_start_nanos,
            "growth_start_nanos",
            minimum=TIMESTAMP_MIN_NS,
            maximum=TIMESTAMP_MAX_NS,
        )
        _integer(self.security_checkpoint_height, "security_checkpoint_height", minimum=0)
        _integer(
            self.security_checkpoint_nanos,
            "security_checkpoint_nanos",
            minimum=TIMESTAMP_MIN_NS,
            maximum=TIMESTAMP_MAX_NS,
        )
        if self.previous_security_checkpoint_height is not None:
            _integer(
                self.previous_security_checkpoint_height,
                "previous_security_checkpoint_height",
                minimum=0,
                maximum=self.security_checkpoint_height,
            )
        if self.target_max_age_duration < self.base_max_age_duration:
            raise ValueError("evidence policy target must cover its base duration")

    @classmethod
    def from_state(cls, value: object):
        if not isinstance(value, Mapping) or type(value.get("version")) is not int:
            raise ValueError("missing or malformed evidence policy (__evidence_policy)")
        expected = {"version", *cls.__dataclass_fields__}
        if set(value) != expected or value["version"] != 2:
            raise ValueError("noncanonical evidence policy: fresh-chain schema version 2 required")
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})

    @classmethod
    def seed(cls, days: object, *, nanos: int = 0, height: int = 0, params: Mapping | None = None):
        evidence = dict(DEFAULT_EVIDENCE_PARAMS if params is None else params)
        if set(evidence) != set(DEFAULT_EVIDENCE_PARAMS):
            raise ValueError("evidence parameters must contain the full limits triple")
        for key, value in evidence.items():
            _integer(value, key, minimum=0 if key == "max_bytes" else 1)
        duration = max(required_evidence_duration(days), evidence["max_age_duration"])
        return cls(
            max_age_num_blocks=evidence["max_age_num_blocks"],
            base_max_age_duration=duration,
            max_bytes=evidence["max_bytes"],
            target_max_age_duration=duration,
            growth_start_nanos=nanos,
            security_checkpoint_height=height,
            security_checkpoint_nanos=nanos,
            previous_security_checkpoint_height=None,
        )

    def to_state(self) -> dict:
        return {"version": 2, **vars(self)}

    def effective_duration(self, nanos: int) -> int:
        _integer(nanos, "block nanos", minimum=self.growth_start_nanos, maximum=TIMESTAMP_MAX_NS)
        return min(
            self.target_max_age_duration,
            self.base_max_age_duration + nanos - self.growth_start_nanos,
        )

    def effective_block_age(self, height: int) -> int:
        _integer(height, "block height", minimum=self.security_checkpoint_height)
        floor = (
            self.previous_security_checkpoint_height
            if self.previous_security_checkpoint_height is not None
            else self.security_checkpoint_height
        )
        return max(self.max_age_num_blocks, height - floor)

    def effective_limits(self, *, nanos: int, height: int) -> dict[str, int]:
        _integer(
            nanos,
            "security checkpoint evaluation time",
            minimum=self.security_checkpoint_nanos,
            maximum=TIMESTAMP_MAX_NS,
        )
        return {
            "max_age_num_blocks": self.effective_block_age(height),
            "max_age_duration": self.effective_duration(nanos),
            "max_bytes": self.max_bytes,
        }

    def comet_json(self, *, nanos: int, height: int) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in self.effective_limits(nanos=nanos, height=height).items()
        }

    def consensus_params(self, *, nanos: int, height: int) -> ConsensusParams:
        limits = self.effective_limits(nanos=nanos, height=height)
        seconds, remainder = divmod(limits["max_age_duration"], 1_000_000_000)
        return ConsensusParams(
            evidence=EvidenceParams(
                max_age_num_blocks=limits["max_age_num_blocks"],
                max_age_duration=Duration(seconds=seconds, nanos=remainder),
                max_bytes=limits["max_bytes"],
            )
        )

    def advance(self, *, nanos: int, height: int, observed_days: list[int]):
        limits = self.effective_limits(nanos=nanos, height=height)
        target = max(
            [self.target_max_age_duration, *(required_evidence_duration(d) for d in observed_days)]
        )
        advanced = self
        if target != self.target_max_age_duration:
            advanced = replace(
                self,
                base_max_age_duration=limits["max_age_duration"],
                target_max_age_duration=target,
                growth_start_nanos=nanos,
            )
        # Target changes extend the still-live epoch BEFORE any rotation.
        # A long idle jump rotates once at actual time, never through synthetic
        # missed epochs that would discard the just-fresh stale parent.
        if nanos - advanced.security_checkpoint_nanos >= target:
            advanced = replace(
                advanced,
                previous_security_checkpoint_height=advanced.security_checkpoint_height,
                security_checkpoint_height=height,
                security_checkpoint_nanos=nanos,
            )
        return advanced


def genesis_evidence_policy(genesis: Mapping) -> EvidencePolicy:
    state = genesis.get("abci_genesis", genesis)
    entries = state.get("genesis", [])
    values = [entry.get("value") for entry in entries if entry.get("key") == EVIDENCE_POLICY_KEY]
    if len(values) != 1:
        raise ValueError("genesis must contain exactly one evidence policy (__evidence_policy)")
    policy = _genesis_policy_value(values[0])
    days = [entry["value"] for entry in entries if entry.get("key") == UNBONDING_PERIOD_KEY]
    if days and (
        len(days) != 1 or required_evidence_duration(days[0]) > policy.target_max_age_duration
    ):
        raise ValueError("genesis evidence policy does not cover unbonding_period_days")
    return policy


def evidence_checkpoint_height(state: Mapping) -> int:
    from xian.checkpoint_validation import canonical_integer

    value = _genesis_number(state.get("number", 0))
    if type(value) is str:
        return canonical_integer(value, "checkpoint height", minimum=0)
    return _integer(value, "checkpoint height", minimum=0)


def evidence_checkpoint_nanos(state: Mapping, policy: EvidencePolicy) -> int:
    if "nanos" not in state and evidence_checkpoint_height(state) > 0:
        raise ValueError("positive-height evidence export requires explicit checkpoint nanos")
    point = _genesis_number(state["nanos"]) if "nanos" in state else policy.growth_start_nanos
    return _integer(
        point, "checkpoint nanos", minimum=policy.growth_start_nanos, maximum=TIMESTAMP_MAX_NS
    )


def validate_genesis_evidence_policy(genesis: Mapping) -> EvidencePolicy:
    policy = genesis_evidence_policy(genesis)
    state = genesis.get("abci_genesis", genesis)
    point = evidence_checkpoint_nanos(state, policy)
    height = evidence_checkpoint_height(state)
    policy.effective_limits(nanos=point, height=height)
    if "consensus_params" in genesis:
        if point != comet_time_nanos(genesis["genesis_time"]):
            raise ValueError("genesis evidence policy checkpoint disagrees with genesis_time")
        if genesis["consensus_params"].get("evidence") != policy.comet_json(
            nanos=point, height=height
        ):
            raise ValueError(
                "genesis consensus evidence parameters disagree with __evidence_policy"
            )
    return policy


def _unbonding_writes(changes):
    for change in changes:
        if change["key"] == UNBONDING_PERIOD_KEY:
            yield validated_unbonding_days(decode_wire_numbers(change["value"]))


def stage_evidence_policy(
    driver, *, nanos: int, height: int, processed_entries: list, applied_patches: list
):
    """Observe every successful intermediate policy, then stage one record.

    A policy can increase, create a longer unbond, and decrease in one block.
    Reading only the final scalar would lose that still-live obligation.
    """
    current = driver.get(EVIDENCE_POLICY_KEY)
    policy = EvidencePolicy.from_state(current)
    days = []
    final_days = driver.get(UNBONDING_PERIOD_KEY)
    if final_days is not None:
        days.append(validated_unbonding_days(final_days))
    for entry in processed_entries:
        result = entry.get("tx_result")
        if result is not None and result["status"] == 0:
            days.extend(_unbonding_writes(result["state"]))
    for patch in applied_patches:
        days.extend(_unbonding_writes(patch["changes"]))
    advanced = policy.advance(nanos=nanos, height=height, observed_days=days)
    if advanced != policy:
        driver.set(EVIDENCE_POLICY_KEY, advanced.to_state())
    # Heights and timestamps are consensus inputs. Always send the full derived
    # triple; neither rooted clocks nor previous local markers churn per block.
    return advanced.consensus_params(nanos=nanos, height=height)


def comet_time_nanos(value: str) -> int:
    """Parse the exact RFC3339 timestamp Comet uses, including all nine digits."""
    timestamp = Timestamp()
    timestamp.FromJsonString(value)
    return timestamp.ToNanoseconds()


def validate_init_evidence_request(req, policy: EvidencePolicy, *, nanos: int, height: int) -> None:
    point = nanos
    if req is None or not req.HasField("time"):
        raise ValueError("InitChain time is required for the evidence retention anchor")
    req.time.ToJsonString()  # Validate the protobuf Timestamp domain as well.
    if req.time.ToNanoseconds() != point:
        raise ValueError("InitChain time disagrees with the evidence retention anchor")
    if req.HasField("consensus_params") and req.consensus_params.HasField("evidence"):
        if (
            req.consensus_params.evidence
            != policy.consensus_params(nanos=point, height=height).evidence
        ):
            raise ValueError("InitChain evidence parameters disagree with __evidence_policy")


def validate_committed_evidence_policy(driver) -> None:
    from xian.constants import Constants
    from xian.fee_policy import FEE_POLICY_KEY

    value = driver.value_from_disk(EVIDENCE_POLICY_KEY)
    marker = driver.value_from_disk(Constants.LATEST_BLOCK_KEY)
    if value is None:
        # Before InitChain, only the execution marker/local helpers may exist.
        # Once genesis was applied, losing this key must never reset retention.
        if marker is not None or driver.value_from_disk(FEE_POLICY_KEY) is not None:
            raise ValueError("committed state is missing its evidence policy (__evidence_policy)")
        return
    policy = EvidencePolicy.from_state(value)
    if marker is not None:
        policy.effective_limits(nanos=marker["nanos"], height=marker["height"])
    days = driver.value_from_disk(UNBONDING_PERIOD_KEY)
    if days is not None and required_evidence_duration(days) > policy.target_max_age_duration:
        raise ValueError("committed evidence policy target does not cover unbonding_period_days")


def _genesis_number(value):
    # Builders/decoded exports already contain runtime ints. Serialized V2
    # genesis carries integers outside JavaScript's safe range as tagged maps.
    return decode_wire_numbers(value) if isinstance(value, Mapping) else value


def _genesis_policy_value(value):
    if isinstance(value, Mapping):
        value = {key: _genesis_number(item) for key, item in value.items()}
    return EvidencePolicy.from_state(value)


def committed_evidence_policy_response(driver) -> dict:
    """Expose the restored app's authenticated policy without pending writes.

    The caller holds the node state lock, which also serializes Commit and
    snapshot replacement. The durable marker and record therefore share one
    committed revision. A bootstrap client must match this height/root/time to
    its trusted light-client state before using the evidence limits.
    """
    import re

    from xian.constants import Constants

    marker = driver.value_from_disk(Constants.LATEST_BLOCK_KEY)
    if not isinstance(marker, Mapping) or set(marker) != {"height", "hash", "nanos"}:
        raise ValueError("evidence policy requires a valid committed block marker")
    height = _integer(marker["height"], "committed height")
    nanos = _integer(
        marker["nanos"], "committed nanos", minimum=TIMESTAMP_MIN_NS, maximum=TIMESTAMP_MAX_NS
    )
    app_hash = marker["hash"]
    if type(app_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", app_hash) is None:
        raise ValueError("evidence policy committed app hash must be lowercase 64-hex")
    policy = EvidencePolicy.from_state(driver.value_from_disk(EVIDENCE_POLICY_KEY))
    policy.effective_limits(nanos=nanos, height=height)
    return {
        "schema_version": 1,
        "height": str(height),
        "app_hash": app_hash,
        "last_block_nanos": str(nanos),
        "evidence": policy.comet_json(nanos=nanos, height=height),
    }


def validate_finalized_time(driver, nanos: int) -> None:
    """Refuse a malformed/backwards ABCI clock before execution starts.

    Stock Comet already validates header time. The durable marker is only a
    local consistency guard here, never an input to retention or update omission.
    """
    from xian.constants import Constants

    _integer(nanos, "block nanos", minimum=TIMESTAMP_MIN_NS, maximum=TIMESTAMP_MAX_NS)
    marker = driver.value_from_disk(Constants.LATEST_BLOCK_KEY)
    if marker is not None:
        previous = _integer(
            marker["nanos"], "committed nanos", minimum=TIMESTAMP_MIN_NS, maximum=TIMESTAMP_MAX_NS
        )
        if nanos < previous:
            raise ValueError("finalized block time is before the last committed checkpoint")
