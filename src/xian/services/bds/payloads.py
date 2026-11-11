from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xian_runtime_types.encoding import decode_wire_numbers

from xian.execution_policy import current_execution_policy, validate_execution_policy
from xian.services.bds.serializer import canonical_json_value as _canonical_json_value


@dataclass(slots=True, frozen=True)
class BdsTransactionPayload:
    tx_index: int
    envelope: dict[str, Any]
    payload: dict[str, Any]
    tx_result: dict[str, Any]


@dataclass(slots=True, frozen=True)
class BdsBlockPayload:
    block_meta: dict[str, Any]
    block_time: datetime
    app_hash: str
    transactions: list[BdsTransactionPayload] = field(default_factory=list)
    state_patches: list[dict[str, Any]] = field(default_factory=list)
    state_patch_hash: str | None = None
    execution_policy: dict[str, str] = field(default_factory=current_execution_policy)

    def to_spool_dict(self) -> dict[str, Any]:
        validate_execution_policy(self.execution_policy)
        return {
            "version": 2,
            "execution_policy": dict(self.execution_policy),
            "block_meta": _canonical_json_value(self.block_meta),
            "block_time": self.block_time.astimezone(UTC).isoformat(),
            "app_hash": self.app_hash,
            "transactions": [
                {
                    "tx_index": tx.tx_index,
                    "envelope": json.loads(json.dumps(tx.envelope)),
                    "payload": json.loads(json.dumps(tx.payload)),
                    "tx_result": _canonical_json_value(tx.tx_result),
                }
                for tx in self.transactions
            ],
            "state_patches": _canonical_json_value(self.state_patches),
            "state_patch_hash": self.state_patch_hash,
        }

    @classmethod
    def from_spool_dict(cls, data: dict[str, Any]) -> BdsBlockPayload:
        if data.get("version") != 2:
            raise ValueError("unsupported BDS spool format version")
        validate_execution_policy(data.get("execution_policy"))
        return cls(
            execution_policy=dict(data["execution_policy"]),
            block_meta=decode_wire_numbers(data["block_meta"]),
            block_time=datetime.fromisoformat(data["block_time"]),
            app_hash=data["app_hash"],
            transactions=[
                BdsTransactionPayload(
                    tx_index=tx["tx_index"],
                    envelope=tx["envelope"],
                    payload=tx["payload"],
                    tx_result=decode_wire_numbers(tx["tx_result"]),
                )
                for tx in data.get("transactions", [])
            ],
            state_patches=decode_wire_numbers(data.get("state_patches", [])),
            state_patch_hash=data.get("state_patch_hash"),
        )
