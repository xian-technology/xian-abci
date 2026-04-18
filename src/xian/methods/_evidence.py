"""
Validator-evidence handling for ``finalize_block``.

Factored out of ``finalize_block.py`` so the evidence / slashing path can be
reasoned about in isolation. The module is private to the ``xian.methods``
package (underscore prefix) — callers should go through
``finalize_block.finalize_block``.
"""

import hashlib

from loguru import logger

from xian.app_logging import build_log_fields
from xian.utils.encoding import encode_abci_json, hash_bytes

SYSTEM_EVIDENCE_SENDER = "__evidence_penalty_driver__"
MISBEHAVIOR_TYPE_NAMES = {
    1: "DUPLICATE_VOTE",
    2: "LIGHT_CLIENT_ATTACK",
}


def _validator_consensus_address(pubkey_hex: str) -> bytes | None:
    try:
        return hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()[:20]
    except ValueError:
        return None


def _resolve_misbehaving_validator_key(
    self, validator_address: bytes
) -> str | None:
    if not validator_address:
        return None
    known_validators = (
        self.client.raw_driver.get(
            "masternodes.validator_registry",
            save=False,
        )
        or self.client.raw_driver.get("masternodes.nodes", save=False)
        or []
    )
    for validator_key in known_validators:
        consensus_address = _validator_consensus_address(validator_key)
        if consensus_address == validator_address:
            return validator_key
    return None


def _misbehavior_type_name(raw_type: int) -> str | None:
    return MISBEHAVIOR_TYPE_NAMES.get(int(raw_type))


def _misbehavior_evidence_id(misbehavior, validator_key: str) -> str:
    evidence_time = getattr(misbehavior, "time", None)
    seconds = getattr(evidence_time, "seconds", 0)
    nanos = getattr(evidence_time, "nanos", 0)
    validator_address = bytes(getattr(misbehavior.validator, "address", b""))
    raw_evidence_id = (
        f"{_misbehavior_type_name(misbehavior.type)}:{validator_key}:"
        f"{validator_address.hex()}:{misbehavior.height}:"
        f"{seconds}:{nanos}:{misbehavior.total_voting_power}"
    )
    return hashlib.sha256(raw_evidence_id.encode("utf-8")).hexdigest()


def maybe_apply_evidence_penalties(self, req, *, height: int) -> bool:
    """
    Apply slashing penalties for any validator misbehavior recorded in the
    ABCI FinalizeBlock request. Returns True if any penalty was applied so
    the caller can mix the evidence writes into the block's fingerprint.
    """
    misbehavior_entries = list(getattr(req, "misbehavior", []) or [])
    if len(misbehavior_entries) == 0:
        return False

    any_applied = False
    for misbehavior in misbehavior_entries:
        infraction_type = _misbehavior_type_name(misbehavior.type)
        if infraction_type is None:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={"misbehavior_type": int(misbehavior.type)},
                )
            ).warning("Ignoring unsupported validator misbehavior type")
            continue

        validator_key = _resolve_misbehaving_validator_key(
            self,
            bytes(misbehavior.validator.address),
        )
        if validator_key is None:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_address": bytes(
                            misbehavior.validator.address
                        ).hex(),
                    },
                )
            ).warning(
                "Could not resolve validator key for misbehavior evidence"
            )
            continue

        evidence_id = _misbehavior_evidence_id(misbehavior, validator_key)
        evidence_tx = {
            "payload": {
                "sender": SYSTEM_EVIDENCE_SENDER,
                "contract": "masternodes",
                "function": "apply_evidence_penalty",
                "kwargs": {
                    "member": validator_key,
                    "infraction_type": infraction_type,
                    "evidence_id": evidence_id,
                    "evidence_height": misbehavior.height,
                },
                "chi_supplied": 0,
            },
            "metadata": {
                "signature": f"evidence-penalty:{evidence_id}",
            },
            "b_meta": self.current_block_meta,
        }
        result = self.tx_processor.process_tx(
            evidence_tx,
            enabled_fees=False,
            rewards_handler=None,
            track_access=False,
        )
        tx_result = result.get("tx_result")
        if tx_result is None or tx_result.get("status") != 0:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    status=(tx_result or {}).get("status"),
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_key": validator_key,
                    },
                )
            ).warning("Evidence penalty did not apply")
            continue

        state_write_count = len(tx_result.get("state", []))
        if state_write_count > 0:
            self.fingerprint_hashes.append(
                hash_bytes(encode_abci_json(tx_result))
            )
            any_applied = True
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_key": validator_key,
                        "state_write_count": state_write_count,
                    },
                )
            ).info("Applied validator evidence penalty")

    return any_applied
