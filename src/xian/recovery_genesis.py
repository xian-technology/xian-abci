"""A recovery genesis for a chain halted by offline voting power.

When validators holding a third or more of the voting power stop signing,
CometBFT cannot commit another block, and no transaction (governance or
downtime jailing) can change the set. The remaining operators then restart
the chain from its last committed state: each stops its node, exports the
state (``xian-export-state``), and everyone builds the same recovery genesis
from it. The genesis keeps the chain id and continues two heights after the
export: the halted chain voted on the height after it and never committed
it, so no height carries votes of both chains (old votes cannot become
duplicate-vote evidence, and every signer may sign the first new height
without resetting its last-signed state). Only the validators kept stay
active, and the others leave the set as validators jailed for downtime do
(power 0, ``approved``, jailed with reason ``downtime`` at the genesis time),
so they return through the usual unjail path once their nodes run again.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any

from xian_runtime_types.time import Datetime

from xian.checkpoint_validation import trusted_checkpoint_point
from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy, comet_time_nanos
from xian.genesis_builder import (
    build_cometbft_genesis,
    build_validator_genesis_entry_from_public_key,
    normalize_genesis_time,
    parse_genesis_time,
)
from xian.state_export import prepare_state_import
from xian.state_root import compute_exported_state_root
from xian.utils.block import nanoseconds_to_utc_datetime
from xian.validators import CONSENSUS_VALIDATORS_KEY

ACTIVE_KEY = "validators.active_validators"


def _validated_keep(active: list[str], keep: list[str]) -> list[str]:
    if not keep:
        raise ValueError("a recovery genesis keeps at least one validator")
    if len(set(keep)) != len(keep):
        raise ValueError("a recovery genesis lists a validator twice (duplicate)")
    unknown = [key for key in keep if key not in active]
    if unknown:
        raise ValueError(f"validators to keep are not active: {unknown}")
    return [key for key in active if key in keep]


def _removed_validator_entries(key: str, jailed_at: Datetime) -> dict[str, Any]:
    return {
        f"validators.powers:{key}": 0,
        f"validators.statuses:{key}": "approved",
        f"validators.jailed:{key}": True,
        f"validators.jail_reasons:{key}": "downtime",
        f"validators.last_jailed_at:{key}": jailed_at,
        f"validators.left_at:{key}": jailed_at,
        f"validators.missed_blocks:{key}": 0,
    }


def _recovered_entries(
    state: dict[str, Any], keep: list[str], jailed_at: Datetime
) -> dict[str, Any]:
    active = state.get(ACTIVE_KEY) or []
    kept = _validated_keep(active, keep)
    powers = state.get(CONSENSUS_VALIDATORS_KEY) or {}
    entries = {
        ACTIVE_KEY: kept,
        CONSENSUS_VALIDATORS_KEY: {key: powers.get(key, 10) for key in kept},
    }
    for key in active:
        if key not in kept:
            entries.update(_removed_validator_entries(key, jailed_at))
    candidates = list(state.get("validators.candidates") or [])
    entries["validators.candidates"] = candidates + [key for key in active if key not in kept]
    return entries


def _require_later_than_export(checkpoint_nanos: int, genesis_time: str) -> None:
    last_block_time = nanoseconds_to_utc_datetime(checkpoint_nanos)
    if comet_time_nanos(normalize_genesis_time(genesis_time)) <= checkpoint_nanos:
        raise ValueError(
            f"the recovery genesis time must be after the last block ({last_block_time})"
        )


def build_recovery_genesis(
    exported_state: dict[str, Any],
    keep: list[str],
    *,
    chain_id: str,
    genesis_time: str,
    trusted_checkpoint: Mapping,
) -> dict[str, Any]:
    """The CometBFT genesis that restarts the chain from ``exported_state``
    (its last committed state) with only the active validators in ``keep``."""
    prepared = prepare_state_import(exported_state)
    expected = trusted_checkpoint_point(trusted_checkpoint)
    if (prepared.height, prepared.state_root.hex(), prepared.nanos) != expected:
        raise ValueError("export does not match the independently trusted checkpoint")
    _require_later_than_export(prepared.nanos, genesis_time)
    abci_genesis = deepcopy(exported_state)
    state = {entry["key"]: entry["value"] for entry in abci_genesis["genesis"]}
    recovery_time = parse_genesis_time(genesis_time)
    state.update(_recovered_entries(state, keep, recovery_time))
    # Comet starts this explicit recovery at its new genesis time. Banking
    # the halted interval would reopen already-expired/pruned history.
    recovery_nanos = comet_time_nanos(normalize_genesis_time(genesis_time))
    policy = EvidencePolicy.from_state(state[EVIDENCE_POLICY_KEY])
    state[EVIDENCE_POLICY_KEY] = replace(
        policy,
        base_max_age_duration=policy.effective_duration(prepared.nanos),
        growth_start_nanos=recovery_nanos,
    ).to_state()
    abci_genesis["nanos"] = recovery_nanos
    abci_genesis["genesis"] = [{"key": key, "value": value} for key, value in sorted(state.items())]
    abci_genesis["origin"] = {"signature": "", "sender": ""}
    abci_genesis["hash"] = compute_exported_state_root(abci_genesis).hex()
    consensus = state[CONSENSUS_VALIDATORS_KEY]
    genesis = build_cometbft_genesis(
        chain_id=chain_id,
        abci_genesis=abci_genesis,
        validators=[
            build_validator_genesis_entry_from_public_key(public_key_hex=key, power=power)
            for key, power in consensus.items()
        ],
        genesis_time=genesis_time,
    )
    # Skip the height the halted chain was voting on (see the module docstring).
    genesis["initial_height"] = str(int(abci_genesis["number"]) + 2)
    return genesis
