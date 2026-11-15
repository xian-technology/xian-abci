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

from copy import deepcopy
from datetime import datetime
from typing import Any

from xian_runtime_types.time import Datetime

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


def _require_later_than_export(exported_state: dict[str, Any], genesis_time: str) -> None:
    last_block_time = nanoseconds_to_utc_datetime(int(exported_state["nanos"]))
    if datetime.fromisoformat(normalize_genesis_time(genesis_time)) <= last_block_time:
        raise ValueError(
            f"the recovery genesis time must be after the last block ({last_block_time})"
        )


def build_recovery_genesis(
    exported_state: dict[str, Any],
    keep: list[str],
    *,
    chain_id: str,
    genesis_time: str,
) -> dict[str, Any]:
    """The CometBFT genesis that restarts the chain from ``exported_state``
    (its last committed state) with only the active validators in ``keep``."""
    prepare_state_import(exported_state)  # refuses a tampered export
    _require_later_than_export(exported_state, genesis_time)
    abci_genesis = deepcopy(exported_state)
    state = {entry["key"]: entry["value"] for entry in abci_genesis["genesis"]}
    state.update(_recovered_entries(state, keep, parse_genesis_time(genesis_time)))
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
