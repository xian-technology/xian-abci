from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracting.artifacts import build_contract_artifacts
from contracting.storage.driver import XIAN_VM_V1_IR_KEY
from loguru import logger
from xian_runtime_types.encoding import convert_dict

from xian.utils.encoding import hash_bytes

STATE_PATCH_BUNDLE_VERSION = 1
DEFAULT_GOVERNANCE_CONTRACT = "governance"
PATCH_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,127}$")


def resolve_state_patch_dir(constants) -> Path:
    return constants.COMETBFT_HOME / "config" / "state-patches"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    hash_obj = hash_bytes(value.encode("utf-8"))
    if isinstance(hash_obj, bytes):
        return hash_obj.hex()
    return hash_obj


def _canonical_change(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment": change.get("comment", ""),
        "key": change["key"],
        "value": change["value"],
    }


def build_contract_artifacts_from_source(
    change: dict[str, Any],
) -> tuple[str, str]:
    contract_name = change["key"].split(".", 1)[0]
    artifacts = build_contract_artifacts(
        module_name=contract_name,
        source=change["value"],
        lint=False,
        vm_profile="xian_vm_v1",
        compact=False,
    )
    return (
        artifacts["source"],
        artifacts["vm_ir_json"],
    )


def hash_from_state_changes(state_changes: list[dict[str, Any]]) -> str:
    serialized_changes = []
    for change in state_changes:
        serialized_changes.append(
            {
                "key": change["key"],
                "value": json.dumps(change["value"], sort_keys=True),
            }
        )

    serialized_changes.sort(key=lambda item: item["key"])
    return _hash_text(_canonical_json(serialized_changes))


def _hash_from_bundle_payload(payload: dict[str, Any]) -> str:
    canonical_changes = sorted(
        (_canonical_change(change) for change in payload["changes"]),
        key=lambda item: item["key"],
    )
    canonical_payload = {
        "activation_height": payload["activation_height"],
        "chain_id": payload.get("chain_id"),
        "changes": canonical_changes,
        "governance_contract": payload["governance_contract"],
        "patch_id": payload["patch_id"],
        "summary": payload.get("summary", ""),
        "uri": payload.get("uri", ""),
        "version": payload["version"],
    }
    return _hash_text(_canonical_json(canonical_payload))


def _hash_from_execution_payload(payload: dict[str, Any]) -> str:
    canonical_changes = sorted(
        (_canonical_change(change) for change in payload["changes"]),
        key=lambda item: item["key"],
    )
    canonical_payload = {
        "activation_height": payload["activation_height"],
        "bundle_hash": payload["bundle_hash"],
        "changes": canonical_changes,
        "emergency": payload["emergency"],
        "governance_contract": payload["governance_contract"],
        "patch_id": payload["patch_id"],
        "proposal_id": payload["proposal_id"],
    }
    return _hash_text(_canonical_json(canonical_payload))


@dataclass(frozen=True, slots=True)
class StatePatchBundle:
    patch_id: str
    activation_height: int
    governance_contract: str
    bundle_hash: str
    summary: str
    uri: str
    chain_id: str | None
    changes: tuple[dict[str, Any], ...]
    file_path: str

    def to_inventory_record(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "activation_height": self.activation_height,
            "governance_contract": self.governance_contract,
            "bundle_hash": self.bundle_hash,
            "summary": self.summary,
            "uri": self.uri,
            "chain_id": self.chain_id,
            "change_count": len(self.changes),
            "file_path": self.file_path,
        }


@dataclass(frozen=True, slots=True)
class ScheduledPatchRecord:
    patch_id: str
    proposal_id: int
    bundle_hash: str
    activation_height: int
    governance_contract: str
    summary: str
    uri: str
    emergency: bool
    status: str

    def to_dict(self, *, local_bundle: StatePatchBundle | None) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "proposal_id": self.proposal_id,
            "bundle_hash": self.bundle_hash,
            "activation_height": self.activation_height,
            "governance_contract": self.governance_contract,
            "summary": self.summary,
            "uri": self.uri,
            "emergency": self.emergency,
            "status": self.status,
            "local_bundle_available": local_bundle is not None,
            "local_file_path": None if local_bundle is None else local_bundle.file_path,
        }


@dataclass(frozen=True, slots=True)
class PatchExecution:
    record: ScheduledPatchRecord
    bundle: StatePatchBundle
    execution_hash: str
    applied_changes: tuple[dict[str, Any], ...]

    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.record.patch_id,
            "proposal_id": self.record.proposal_id,
            "bundle_hash": self.record.bundle_hash,
            "execution_hash": self.execution_hash,
            "activation_height": self.record.activation_height,
            "governance_contract": self.record.governance_contract,
            "summary": self.record.summary,
            "uri": self.record.uri,
            "emergency": self.record.emergency,
            "changes": [dict(change) for change in self.applied_changes],
        }


class StatePatchManager:
    def __init__(
        self,
        raw_driver,
        *,
        chain_id: str | None = None,
        governance_contract: str = DEFAULT_GOVERNANCE_CONTRACT,
    ):
        self.raw_driver = raw_driver
        self.chain_id = chain_id
        self.governance_contract = governance_contract
        self.local_bundles: dict[str, StatePatchBundle] = {}
        self.loaded = False

    def load_patches(self, patch_path: str | Path) -> None:
        path = Path(patch_path)
        if not path.exists():
            logger.info("No state patch bundle path found at {}", path)
            self.local_bundles = {}
            self.loaded = True
            return

        bundle_files = [path] if path.is_file() else sorted(path.glob("*.json"))
        bundles: dict[str, StatePatchBundle] = {}
        try:
            for bundle_file in bundle_files:
                bundle = self._load_bundle_file(bundle_file)
                if bundle.patch_id in bundles:
                    raise ValueError(f"duplicate patch_id '{bundle.patch_id}' in bundle inventory")
                bundles[bundle.patch_id] = bundle
        except Exception:
            self.local_bundles = {}
            self.loaded = False
            logger.exception("Error loading state patch bundles from {}", path)
            raise

        self.local_bundles = bundles
        self.loaded = True
        logger.info(
            "Loaded {} local state patch bundle(s) from {}",
            len(self.local_bundles),
            path,
        )

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise RuntimeError("State patch bundle inventory is not loaded")

    def _load_bundle_file(self, bundle_file: Path) -> StatePatchBundle:
        payload = json.loads(bundle_file.read_text(encoding="utf-8"))
        if payload.get("version") != STATE_PATCH_BUNDLE_VERSION:
            raise ValueError(f"{bundle_file} has unsupported state patch bundle version")

        patch_id = payload.get("patch_id")
        if not isinstance(patch_id, str) or not PATCH_ID_PATTERN.fullmatch(patch_id):
            raise ValueError(f"{bundle_file} has invalid patch_id")

        activation_height = payload.get("activation_height")
        if not isinstance(activation_height, int) or activation_height <= 0:
            raise ValueError(f"{bundle_file} must set a positive activation_height")

        governance_contract = payload.get("governance_contract", self.governance_contract)
        if not isinstance(governance_contract, str) or governance_contract == "":
            raise ValueError(f"{bundle_file} has invalid governance_contract")

        chain_id = payload.get("chain_id")
        if chain_id is not None and (not isinstance(chain_id, str) or chain_id == ""):
            raise ValueError(f"{bundle_file} has invalid chain_id")

        changes = payload.get("changes")
        if not isinstance(changes, list) or len(changes) == 0:
            raise ValueError(f"{bundle_file} must define a non-empty changes list")

        normalized_changes: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for raw_change in changes:
            if not isinstance(raw_change, dict):
                raise ValueError(f"{bundle_file} contains a non-object patch change")
            key = raw_change.get("key")
            if not isinstance(key, str) or key == "":
                raise ValueError(f"{bundle_file} contains a patch change with no key")
            if key in seen_keys:
                raise ValueError(f"{bundle_file} contains duplicate key {key!r}")
            if key.endswith(".__code__"):
                raise ValueError(f"{bundle_file} cannot patch __code__ directly; use __source__")
            if key.endswith(f".{XIAN_VM_V1_IR_KEY}"):
                raise ValueError(
                    f"{bundle_file} cannot patch {XIAN_VM_V1_IR_KEY} directly; use __source__"
                )
            seen_keys.add(key)
            normalized_changes.append(
                {
                    "key": key,
                    "value": raw_change.get("value"),
                    "comment": raw_change.get("comment", ""),
                }
            )

        bundle_hash = _hash_from_bundle_payload(
            {
                "version": payload["version"],
                "patch_id": patch_id,
                "activation_height": activation_height,
                "governance_contract": governance_contract,
                "chain_id": chain_id,
                "summary": payload.get("summary", ""),
                "uri": payload.get("uri", ""),
                "changes": normalized_changes,
            }
        )

        return StatePatchBundle(
            patch_id=patch_id,
            activation_height=activation_height,
            governance_contract=governance_contract,
            bundle_hash=bundle_hash,
            summary=payload.get("summary", ""),
            uri=payload.get("uri", ""),
            chain_id=chain_id,
            changes=tuple(normalized_changes),
            file_path=str(bundle_file.resolve()),
        )

    def get_local_bundle_inventory(self) -> list[dict[str, Any]]:
        self._require_loaded()
        return [
            bundle.to_inventory_record()
            for bundle in sorted(self.local_bundles.values(), key=lambda item: item.patch_id)
        ]

    def _read_scheduled_patch_records(
        self,
        *,
        height: int,
        include_applied: bool,
    ) -> list[ScheduledPatchRecord]:
        if self.raw_driver is None:
            return []

        prefix = f"{self.governance_contract}.scheduled_patches:{height}:"
        scheduled_items = self.raw_driver.items(prefix)
        records: list[ScheduledPatchRecord] = []
        for key, scheduled in scheduled_items.items():
            if not scheduled:
                continue
            patch_id = key[len(prefix) :]
            status = self.raw_driver.get_var(
                self.governance_contract, "patches", [patch_id, "status"]
            )
            if status not in {"approved", "applied"}:
                continue
            if status == "applied" and not include_applied:
                continue
            proposal_id = self.raw_driver.get_var(
                self.governance_contract, "patches", [patch_id, "proposal_id"]
            )
            bundle_hash = self.raw_driver.get_var(
                self.governance_contract, "patches", [patch_id, "bundle_hash"]
            )
            activation_height = self.raw_driver.get_var(
                self.governance_contract,
                "patches",
                [patch_id, "activation_height"],
            )
            summary = (
                self.raw_driver.get_var(self.governance_contract, "patches", [patch_id, "summary"])
                or ""
            )
            uri = (
                self.raw_driver.get_var(self.governance_contract, "patches", [patch_id, "uri"])
                or ""
            )
            emergency = bool(
                self.raw_driver.get_var(
                    self.governance_contract, "patches", [patch_id, "emergency"]
                )
            )
            if not isinstance(proposal_id, int):
                raise ValueError(f"missing proposal_id for governed state patch {patch_id}")
            if not isinstance(bundle_hash, str) or bundle_hash == "":
                raise ValueError(f"missing bundle_hash for governed state patch {patch_id}")
            if activation_height != height:
                raise ValueError(
                    f"governed state patch {patch_id} has inconsistent activation height"
                )
            records.append(
                ScheduledPatchRecord(
                    patch_id=patch_id,
                    proposal_id=proposal_id,
                    bundle_hash=bundle_hash,
                    activation_height=activation_height,
                    governance_contract=self.governance_contract,
                    summary=summary,
                    uri=uri,
                    emergency=emergency,
                    status=status,
                )
            )

        records.sort(key=lambda item: item.patch_id)
        return records

    def get_scheduled_patch_inventory(self, height: int) -> list[dict[str, Any]]:
        self._require_loaded()
        records = self._read_scheduled_patch_records(
            height=height,
            include_applied=True,
        )
        return [
            record.to_dict(local_bundle=self.local_bundles.get(record.patch_id))
            for record in records
        ]

    def _validate_bundle_against_record(
        self,
        bundle: StatePatchBundle,
        record: ScheduledPatchRecord,
    ) -> None:
        if bundle.bundle_hash != record.bundle_hash:
            raise ValueError(
                f"state patch bundle hash mismatch for {record.patch_id}: "
                f"local={bundle.bundle_hash} governed={record.bundle_hash}"
            )
        if bundle.activation_height != record.activation_height:
            raise ValueError(f"state patch activation height mismatch for {record.patch_id}")
        if bundle.governance_contract != record.governance_contract:
            raise ValueError(f"state patch governance contract mismatch for {record.patch_id}")
        if bundle.chain_id is not None and self.chain_id != bundle.chain_id:
            raise ValueError(f"state patch {record.patch_id} targets chain_id {bundle.chain_id!r}")

    def _build_applied_changes(self, bundle: StatePatchBundle) -> tuple[dict[str, Any], ...]:
        applied_changes: list[dict[str, Any]] = []
        for change in bundle.changes:
            applied_changes.append(dict(change))
            parts = change["key"].split(".")
            if len(parts) > 1 and parts[1] == "__source__":
                contract_name = parts[0]
                normalized_source, vm_ir_json = build_contract_artifacts_from_source(change)
                applied_changes[-1]["value"] = normalized_source
                applied_changes.append(
                    {
                        "key": f"{contract_name}.{XIAN_VM_V1_IR_KEY}",
                        "value": vm_ir_json,
                        "comment": f"Persisted VM IR for {change.get('comment', '')}",
                    }
                )

        applied_changes.sort(key=lambda item: item["key"])
        return tuple(applied_changes)

    def _build_executions(
        self,
        *,
        height: int,
        include_applied: bool,
    ) -> list[PatchExecution]:
        self._require_loaded()

        records = self._read_scheduled_patch_records(
            height=height,
            include_applied=include_applied,
        )
        executions: list[PatchExecution] = []
        for record in records:
            bundle = self.local_bundles.get(record.patch_id)
            if bundle is None:
                raise FileNotFoundError(
                    f"missing local state patch bundle for approved patch {record.patch_id}"
                )
            self._validate_bundle_against_record(bundle, record)
            applied_changes = self._build_applied_changes(bundle)
            execution_hash = _hash_from_execution_payload(
                {
                    "patch_id": record.patch_id,
                    "proposal_id": record.proposal_id,
                    "bundle_hash": record.bundle_hash,
                    "activation_height": record.activation_height,
                    "governance_contract": record.governance_contract,
                    "emergency": record.emergency,
                    "changes": applied_changes,
                }
            )
            executions.append(
                PatchExecution(
                    record=record,
                    bundle=bundle,
                    execution_hash=execution_hash,
                    applied_changes=applied_changes,
                )
            )
        return executions

    def build_applied_patches_for_block(
        self, height: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        executions = self._build_executions(height=height, include_applied=True)
        if not executions:
            return None, []
        aggregate_hash = _hash_text(
            _canonical_json([execution.execution_hash for execution in executions])
        )
        return aggregate_hash, [execution.to_payload_dict() for execution in executions]

    def _apply_single_bundle(self, execution: PatchExecution) -> None:
        logger.info(
            "Applying governed state patch {} from {}",
            execution.record.patch_id,
            execution.bundle.file_path,
        )
        for change in execution.bundle.changes:
            key = change["key"]
            value = change["value"]

            parts = key.split(".")
            if len(parts) > 1 and parts[1] == "__source__":
                contract_name = parts[0]
                normalized_source, vm_ir_json = build_contract_artifacts_from_source(change)
                self.raw_driver.set(key, normalized_source)
                self.raw_driver.set(
                    f"{contract_name}.{XIAN_VM_V1_IR_KEY}",
                    vm_ir_json,
                )
                continue

            if isinstance(value, dict):
                value = convert_dict(value)
            self.raw_driver.set(key, value)

    def _mark_patch_applied(
        self,
        execution: PatchExecution,
        *,
        height: int,
        nanos: int,
        block_hash: str | None,
    ) -> None:
        self.raw_driver.set_var(
            self.governance_contract,
            "patches",
            [execution.record.patch_id, "status"],
            "applied",
        )
        self.raw_driver.set_var(
            self.governance_contract,
            "patches",
            [execution.record.patch_id, "applied_block_height"],
            height,
        )
        self.raw_driver.set_var(
            self.governance_contract,
            "patches",
            [execution.record.patch_id, "applied_block_hash"],
            block_hash,
        )
        self.raw_driver.set_var(
            self.governance_contract,
            "patches",
            [execution.record.patch_id, "applied_at_nanos"],
            nanos,
        )
        self.raw_driver.set_var(
            self.governance_contract,
            "patches",
            [execution.record.patch_id, "execution_hash"],
            execution.execution_hash,
        )

    def apply_patches_for_block(
        self,
        height: int,
        nanos: int,
        *,
        block_hash: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        executions = self._build_executions(height=height, include_applied=False)
        if not executions:
            return None, []

        for execution in executions:
            self._apply_single_bundle(execution)
            self._mark_patch_applied(
                execution,
                height=height,
                nanos=nanos,
                block_hash=block_hash,
            )

        aggregate_hash = _hash_text(
            _canonical_json([execution.execution_hash for execution in executions])
        )
        logger.info(
            "Applied {} governed state patch bundle(s) for block {}",
            len(executions),
            height,
        )
        return aggregate_hash, [execution.to_payload_dict() for execution in executions]
