from __future__ import annotations

import json
from typing import Any


def _normalize_hex_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("hex value must be a string")
    trimmed = value[2:] if value.startswith("0x") else value
    return bytes.fromhex(trimmed)


def _payload_ciphertexts(payload_hex: str | None) -> list[dict[str, Any]]:
    if not isinstance(payload_hex, str) or payload_hex == "":
        return []
    try:
        decoded = json.loads(_normalize_hex_bytes(payload_hex).decode("utf-8"))
    except Exception:
        return []
    if not isinstance(decoded, dict):
        return []
    ciphertexts = decoded.get("ciphertexts")
    if not isinstance(ciphertexts, list):
        return []
    return [item for item in ciphertexts if isinstance(item, dict)]


def _unpack_blob(blob: Any) -> list[str]:
    if not isinstance(blob, str) or blob == "":
        return []
    return [item for item in blob.split("|") if item != ""]


def extract_payload_tags(payload_hex: str | None) -> list[dict[str, str]]:
    ciphertexts = _payload_ciphertexts(payload_hex)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in ciphertexts:
        for tag_kind in ("sync_hint", "discovery_tag"):
            tag = item.get(tag_kind)
            if not isinstance(tag, str) or tag == "":
                continue
            key = (tag_kind, tag)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"tag_kind": tag_kind, "tag_value": tag})
    return rows


def collect_shielded_output_tags(
    *,
    contract: str,
    function: str,
    tx_hash: str,
    block_height: int,
    tx_index: int,
    tx_result_events: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    output_payloads = kwargs.get("output_payloads")
    if not isinstance(output_payloads, list) or len(output_payloads) == 0:
        return []

    rows: list[dict[str, Any]] = []
    for event in tx_result_events:
        if not isinstance(event, dict):
            continue
        if event.get("contract") != contract:
            continue
        event_name = event.get("event")
        data_indexed = event.get("data_indexed") or {}
        data = event.get("data") or {}

        output_specs: list[dict[str, Any]] = []
        action = data.get("action")
        new_root = data_indexed.get("new_root")

        if event_name == "ShieldedOutputCommitted":
            output_index = data.get("output_index")
            note_index = data.get("note_index")
            payload_hash = data.get("payload_hash")
            commitment = data_indexed.get("commitment")
            if not isinstance(output_index, int):
                continue
            output_specs.append(
                {
                    "output_index": output_index,
                    "note_index": note_index if isinstance(note_index, int) else None,
                    "payload_hash": payload_hash if isinstance(payload_hash, str) else "",
                    "commitment": commitment if isinstance(commitment, str) else "",
                }
            )
        elif event_name == "ShieldedOutputsCommitted":
            note_index_start = data.get("note_index_start")
            output_count = data.get("output_count")
            commitments = _unpack_blob(data.get("commitments_blob"))
            payload_hashes = _unpack_blob(data.get("payload_hashes_blob"))
            if not isinstance(note_index_start, int):
                continue
            if not isinstance(output_count, int):
                output_count = len(commitments)
            if len(commitments) < output_count or len(payload_hashes) < output_count:
                continue
            for output_index in range(output_count):
                output_specs.append(
                    {
                        "output_index": output_index,
                        "note_index": note_index_start + output_index,
                        "payload_hash": payload_hashes[output_index],
                        "commitment": commitments[output_index],
                    }
                )
        else:
            continue

        for output_spec in output_specs:
            output_index = output_spec["output_index"]
            if output_index < 0 or output_index >= len(output_payloads):
                continue
            for payload_tag in extract_payload_tags(output_payloads[output_index]):
                rows.append(
                    {
                        "tx_hash": tx_hash,
                        "block_height": block_height,
                        "tx_index": tx_index,
                        "contract": contract,
                        "function": function,
                        "action": action if isinstance(action, str) else function,
                        "output_index": output_index,
                        "note_index": output_spec["note_index"],
                        "commitment": output_spec["commitment"],
                        "new_root": new_root if isinstance(new_root, str) else "",
                        "payload_hash": output_spec["payload_hash"],
                        "tag_kind": payload_tag["tag_kind"],
                        "tag_value": payload_tag["tag_value"],
                    }
                )
    return rows
