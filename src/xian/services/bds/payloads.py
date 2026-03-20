from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
