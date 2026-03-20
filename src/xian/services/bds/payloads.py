from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xian_runtime_types.encoding import encode as encode_runtime_value


def _canonical_json_value(value: Any) -> Any:
    return json.loads(encode_runtime_value(value))


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

    def to_spool_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "block_meta": _canonical_json_value(self.block_meta),
            "block_time": self.block_time.astimezone(UTC).isoformat(),
            "app_hash": self.app_hash,
            "transactions": [
                {
                    "tx_index": tx.tx_index,
                    "envelope": _canonical_json_value(tx.envelope),
                    "payload": _canonical_json_value(tx.payload),
                    "tx_result": _canonical_json_value(tx.tx_result),
                }
                for tx in self.transactions
            ],
            "state_patches": _canonical_json_value(self.state_patches),
            "state_patch_hash": self.state_patch_hash,
        }

    @classmethod
    def from_spool_dict(cls, data: dict[str, Any]) -> BdsBlockPayload:
        return cls(
            block_meta=data["block_meta"],
            block_time=datetime.fromisoformat(data["block_time"]),
            app_hash=data["app_hash"],
            transactions=[
                BdsTransactionPayload(
                    tx_index=tx["tx_index"],
                    envelope=tx["envelope"],
                    payload=tx["payload"],
                    tx_result=tx["tx_result"],
                )
                for tx in data.get("transactions", [])
            ],
            state_patches=data.get("state_patches", []),
            state_patch_hash=data.get("state_patch_hash"),
        )
