from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from time import time
from typing import Any

from loguru import logger
from xian_runtime_types.encoding import safe_repr

from xian.app_logging import build_log_fields
from xian.utils.encoding import normalize_for_abci_json, stringify_decimals


def _normalize_value(value: Any) -> Any:
    if isinstance(value, BaseException):
        return safe_repr(value)
    return stringify_decimals(normalize_for_abci_json(value))


def _normalize_mismatches(
    mismatches: dict[str, tuple[Any, Any]] | None,
) -> dict[str, list[Any]]:
    if not mismatches:
        return {}
    normalized: dict[str, list[Any]] = {}
    for field, pair in mismatches.items():
        authoritative, native = pair
        normalized[field] = [
            _normalize_value(authoritative),
            _normalize_value(native),
        ]
    return normalized


class VmShadowObserver:
    def __init__(
        self,
        *,
        enabled: bool,
        mode: str,
        authority: str,
        shadow_execution: bool,
        mismatch_log_path: Path | None,
        recent_mismatch_limit: int = 20,
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.authority = authority
        self.shadow_execution = shadow_execution
        self.mismatch_log_path = mismatch_log_path
        self._recent_mismatch_limit = max(int(recent_mismatch_limit), 1)
        self._lock = threading.Lock()
        self._comparisons_total = 0
        self._mismatches_total = 0
        self._last_comparison_at_unix: float | None = None
        self._last_mismatch_at_unix: float | None = None
        self._stage_stats: dict[str, dict[str, int]] = {}
        self._recent_mismatches: deque[dict[str, Any]] = deque(
            maxlen=self._recent_mismatch_limit
        )

    @classmethod
    def for_runtime(
        cls,
        *,
        storage_home: Path,
        mode: str,
        authority: str,
        shadow_execution: bool,
    ) -> VmShadowObserver:
        enabled = mode == "xian_vm_v1" and (
            shadow_execution or authority == "native"
        )
        mismatch_log_path = (
            storage_home / "logs" / "xian-vm-shadow-mismatches.jsonl"
            if enabled
            else None
        )
        return cls(
            enabled=enabled,
            mode=mode,
            authority=authority,
            shadow_execution=shadow_execution,
            mismatch_log_path=mismatch_log_path,
        )

    def record_comparison(
        self,
        *,
        stage: str,
        contract: str,
        function: str,
        sender: str | None = None,
        nonce: int | None = None,
        tx_hash: str | None = None,
        block_height: int | None = None,
        mismatches: dict[str, tuple[Any, Any]] | None = None,
    ) -> None:
        if not self.enabled:
            return

        now_unix = time()
        mismatch_fields = sorted((mismatches or {}).keys())
        normalized_mismatches = _normalize_mismatches(mismatches)
        record = {
            "timestamp_unix": now_unix,
            "stage": stage,
            "contract": contract,
            "function": function,
            "sender": sender,
            "nonce": nonce,
            "tx_hash": tx_hash,
            "block_height": block_height,
            "mismatch_fields": mismatch_fields,
            "mismatches": normalized_mismatches,
        }

        with self._lock:
            self._comparisons_total += 1
            self._last_comparison_at_unix = now_unix
            stage_stats = self._stage_stats.setdefault(
                stage,
                {"comparisons_total": 0, "mismatches_total": 0},
            )
            stage_stats["comparisons_total"] += 1
            if mismatch_fields:
                self._mismatches_total += 1
                self._last_mismatch_at_unix = now_unix
                stage_stats["mismatches_total"] += 1
                self._recent_mismatches.append(record)

        if mismatch_fields:
            self._append_mismatch_record(record)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            recent = list(self._recent_mismatches)
            latest = recent[-1] if recent else None
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "authority": self.authority,
                "shadow_execution": self.shadow_execution,
                "comparisons_total": self._comparisons_total,
                "mismatches_total": self._mismatches_total,
                "last_comparison_at_unix": self._last_comparison_at_unix,
                "last_mismatch_at_unix": self._last_mismatch_at_unix,
                "stages": {
                    stage: dict(values)
                    for stage, values in sorted(self._stage_stats.items())
                },
                "recent_mismatches": recent,
                "latest_mismatch": latest,
            }

    def _append_mismatch_record(self, record: dict[str, Any]) -> None:
        if self.mismatch_log_path is None:
            return
        try:
            self.mismatch_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.mismatch_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, sort_keys=True, default=str) + "\n"
                )
        except Exception as exc:  # pragma: no cover - defensive logging only
            logger.bind(
                **build_log_fields(
                    stage="vm_shadow_observer",
                    extra={"mismatch_log_path": str(self.mismatch_log_path)},
                )
            ).warning("Failed to append VM shadow mismatch record: {}", exc)
