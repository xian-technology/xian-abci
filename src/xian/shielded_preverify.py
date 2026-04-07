from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracting.stdlib.bridge import zk as zk_bridge

try:
    from xian_zk import (
        canonicalize_command_payload as _canonicalize_command_payload,
    )
except ImportError:  # pragma: no cover - exercised when zk extras are absent
    _canonicalize_command_payload = None


@dataclass(frozen=True)
class ShieldedPreverifyStats:
    candidate_count: int = 0
    verified_count: int = 0
    failed_count: int = 0


def _contract_var(driver, contract: str, variable: str, arguments: list[str]):
    return driver.get_var(contract, variable, arguments)


def _vk_id_for(driver, contract: str, action: str) -> str | None:
    vk_id = _contract_var(driver, contract, "vk_ids", [action])
    if isinstance(vk_id, str) and vk_id != "":
        return vk_id
    return None


def _normalize_output_payloads(
    output_payloads: Any,
    expected_count: int,
) -> list[str]:
    if output_payloads is None:
        return [""] * expected_count
    if not isinstance(output_payloads, list) or len(output_payloads) != expected_count:
        raise AssertionError("output_payloads length must match output commitments")

    normalized = []
    for payload in output_payloads:
        if payload in (None, ""):
            normalized.append("")
        else:
            normalized.append(payload)
    return normalized


def _payload_hashes(output_payloads: list[str]) -> list[str]:
    return zk_bridge.shielded_output_payload_hashes(output_payloads)


def _text_field_hash(value: str) -> str:
    return _payload_hashes(["0x" + value.encode("utf-8").hex()])[0]


def _note_token_entry(
    *,
    driver,
    tx: dict,
) -> dict[str, Any] | None:
    payload = tx.get("payload")
    if not isinstance(payload, dict):
        return None
    contract = payload.get("contract")
    function = payload.get("function")
    sender = payload.get("sender")
    kwargs = payload.get("kwargs")
    if not isinstance(contract, str) or not isinstance(function, str):
        return None
    if not isinstance(sender, str) or sender == "":
        return None
    if not isinstance(kwargs, dict):
        return None

    if function == "deposit_shielded":
        vk_id = _vk_id_for(driver, contract, "deposit")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_deposit_public_inputs(
                contract,
                kwargs["old_root"],
                kwargs["amount"],
                commitments,
                payload_hashes,
            ),
        }

    if function == "transfer_shielded":
        vk_id = _vk_id_for(driver, contract, "transfer")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_transfer_public_inputs(
                contract,
                kwargs["old_root"],
                kwargs["input_nullifiers"],
                commitments,
                payload_hashes,
            ),
        }

    if function == "relay_transfer_shielded":
        vk_id = _vk_id_for(driver, contract, "relay_transfer")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        input_nullifiers = kwargs["input_nullifiers"]
        fee = kwargs.get("relayer_fee", 0)
        expires_at = kwargs.get("expires_at")
        nullifier_digest = zk_bridge.shielded_command_nullifier_digest(
            input_nullifiers
        )
        relay_binding = zk_bridge.shielded_command_binding(
            nullifier_digest,
            _text_field_hash("shielded-note-relay-transfer"),
            _text_field_hash("transfer"),
            _text_field_hash(sender),
            (
                "0x" + "00" * 32
                if expires_at in (None, "")
                else _text_field_hash(str(expires_at))
            ),
            _text_field_hash(tx["b_meta"]["chain_id"]),
            _text_field_hash("relay_transfer_shielded"),
            _text_field_hash("shielded-note-relay-v1"),
            fee,
            0,
        )
        execution_tag = zk_bridge.shielded_command_execution_tag(
            nullifier_digest,
            relay_binding,
        )
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_command_public_inputs(
                contract,
                kwargs["old_root"],
                relay_binding,
                execution_tag,
                fee,
                0,
                input_nullifiers,
                commitments,
                payload_hashes,
            ),
        }

    if function == "withdraw_shielded":
        vk_id = _vk_id_for(driver, contract, "withdraw")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_withdraw_public_inputs(
                contract,
                kwargs["old_root"],
                kwargs["amount"],
                kwargs["to"],
                kwargs["input_nullifiers"],
                commitments,
                payload_hashes,
            ),
        }

    return None


def _command_payload_digest(payload: Any) -> str:
    if _canonicalize_command_payload is None:
        raise AssertionError("xian_zk command payload canonicalizer is unavailable")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise AssertionError("payload must be a dict")
    canonical = _canonicalize_command_payload(payload)
    return _text_field_hash(canonical)


def _shielded_commands_entry(
    *,
    driver,
    tx: dict,
) -> dict[str, Any] | None:
    payload = tx.get("payload")
    if not isinstance(payload, dict):
        return None
    contract = payload.get("contract")
    function = payload.get("function")
    sender = payload.get("sender")
    kwargs = payload.get("kwargs")
    if not isinstance(contract, str) or not isinstance(function, str):
        return None
    if not isinstance(sender, str) or sender == "":
        return None
    if not isinstance(kwargs, dict):
        return None

    if function == "deposit_shielded":
        vk_id = _vk_id_for(driver, contract, "deposit")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_deposit_public_inputs(
                contract,
                kwargs["old_root"],
                kwargs["amount"],
                commitments,
                payload_hashes,
            ),
        }

    if function == "execute_command":
        vk_id = _vk_id_for(driver, contract, "command")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        input_nullifiers = kwargs["input_nullifiers"]
        fee = kwargs.get("relayer_fee", 0)
        public_amount = kwargs.get("public_amount", 0)
        expires_at = kwargs.get("expires_at")
        nullifier_digest = zk_bridge.shielded_command_nullifier_digest(
            input_nullifiers
        )
        binding = zk_bridge.shielded_command_binding(
            nullifier_digest,
            _text_field_hash(kwargs["target_contract"]),
            _command_payload_digest(kwargs.get("payload")),
            _text_field_hash(sender),
            (
                "0x" + "00" * 32
                if expires_at in (None, "")
                else _text_field_hash(str(expires_at))
            ),
            _text_field_hash(tx["b_meta"]["chain_id"]),
            _text_field_hash("interact"),
            _text_field_hash("shielded-command-v4"),
            fee,
            public_amount,
        )
        execution_tag = zk_bridge.shielded_command_execution_tag(
            nullifier_digest,
            binding,
        )
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_command_public_inputs(
                contract,
                kwargs["old_root"],
                binding,
                execution_tag,
                fee,
                public_amount,
                input_nullifiers,
                commitments,
                payload_hashes,
            ),
        }

    if function == "withdraw_shielded":
        vk_id = _vk_id_for(driver, contract, "withdraw")
        if vk_id is None:
            return None
        commitments = kwargs["output_commitments"]
        output_payloads = _normalize_output_payloads(
            kwargs.get("output_payloads"),
            len(commitments),
        )
        payload_hashes = _payload_hashes(output_payloads)
        return {
            "vk_id": vk_id,
            "proof_hex": kwargs["proof_hex"],
            "public_inputs": zk_bridge.shielded_withdraw_public_inputs(
                contract,
                kwargs["old_root"],
                kwargs["amount"],
                kwargs["to"],
                kwargs["input_nullifiers"],
                commitments,
                payload_hashes,
            ),
        }

    return None


def build_verification_request(driver, tx: dict) -> dict[str, Any] | None:
    payload = tx.get("payload")
    if not isinstance(payload, dict):
        return None
    kwargs = payload.get("kwargs")
    if not isinstance(kwargs, dict) or "proof_hex" not in kwargs:
        return None

    request = _note_token_entry(driver=driver, tx=tx)
    if request is not None:
        return request
    return _shielded_commands_entry(driver=driver, tx=tx)


def warm_shielded_proof_cache(
    *,
    driver,
    txs: list[dict],
) -> ShieldedPreverifyStats:
    if not zk_bridge.is_available():
        return ShieldedPreverifyStats()

    requests = []
    for tx in txs:
        try:
            request = build_verification_request(driver, tx)
        except Exception:
            continue
        if request is not None:
            requests.append(request)

    if not requests:
        return ShieldedPreverifyStats()

    results = zk_bridge.warm_verified_proofs(requests)
    verified_count = sum(1 for result in results if result is True)
    failed_count = len(results) - verified_count
    return ShieldedPreverifyStats(
        candidate_count=len(results),
        verified_count=verified_count,
        failed_count=failed_count,
    )
