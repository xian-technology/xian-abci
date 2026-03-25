from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from contracting.client import ContractingClient
from contracting.execution.executor import Executor
from loguru import logger
from xian_runtime_types.encoding import convert_dict, safe_repr
from xian_runtime_types.time import Datetime

from xian.utils.block import (
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import stringify_decimals
from xian.utils.tx import format_dictionary


class TransactionSimulator:
    def __init__(
        self,
        *,
        client: ContractingClient,
        get_block_meta=None,
    ):
        self.client = client
        self.get_block_meta = get_block_meta or (lambda: None)
        self.executor = Executor(
            driver=self.client.raw_driver,
            metering=False,
            bypass_balance_amount=True,
        )

    def simulate(self, payload: dict) -> dict:
        normalized_payload = self._normalize_payload(payload)
        try:
            return self._execute(normalized_payload)
        except Exception as exc:
            logger.error(f"Simulation failed: {exc}")
            return {
                "payload": normalized_payload,
                "status": 1,
                "state": [],
                "stamps_used": 0,
                "result": f"Simulation error: {exc}",
            }

    def simulate_encoded_transaction(self, raw_payload_hex: str) -> dict:
        decoded = json.loads(bytes.fromhex(raw_payload_hex).decode("utf-8"))
        return self.simulate(decoded)

    def _execute(self, payload: dict) -> dict:
        state_snapshot = self._snapshot_driver_state()
        try:
            stamp_cost = int(
                self.client.get_var(
                    contract="stamp_cost",
                    variable="S",
                    arguments=["value"],
                )
            )
        except Exception:
            stamp_cost = 20

        try:
            output = self.executor.execute(
                sender=payload["sender"],
                contract_name=payload["contract"],
                function_name=payload["function"],
                stamps=9_999_999 * stamp_cost,
                stamp_cost=stamp_cost,
                kwargs=convert_dict(payload.get("kwargs", {})),
                environment=self._make_environment(payload),
                auto_commit=False,
                metering=True,
            )

            writes = [
                {"key": key, "value": value}
                for key, value in output["writes"].items()
            ]

            result = {
                "payload": payload,
                "status": output["status_code"],
                "state": writes,
                "stamps_used": output["stamps_used"],
                "result": safe_repr(output["result"]),
            }
            return stringify_decimals(format_dictionary(result))
        finally:
            self._restore_driver_state(state_snapshot)

    def _snapshot_driver_state(self) -> dict:
        return {
            "pending_writes": deepcopy(self.client.raw_driver.pending_writes),
            "pending_reads": deepcopy(self.client.raw_driver.pending_reads),
            "pending_deltas": deepcopy(self.client.raw_driver.pending_deltas),
            "transaction_reads": deepcopy(
                self.client.raw_driver.transaction_reads
            ),
            "transaction_writes": deepcopy(
                self.client.raw_driver.transaction_writes
            ),
            "log_events": deepcopy(self.client.raw_driver.log_events),
        }

    def _restore_driver_state(self, state_snapshot: dict) -> None:
        driver = self.client.raw_driver
        driver.pending_writes = state_snapshot["pending_writes"]
        driver.pending_reads = state_snapshot["pending_reads"]
        driver.pending_deltas = state_snapshot["pending_deltas"]
        driver.transaction_reads = state_snapshot["transaction_reads"]
        driver.transaction_writes = state_snapshot["transaction_writes"]
        driver.log_events = state_snapshot["log_events"]

    @staticmethod
    def _normalize_payload(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("simulation payload must be a JSON object")

        normalized = payload.get("payload", payload)
        if not isinstance(normalized, dict):
            raise ValueError("simulation payload must resolve to a JSON object")

        kwargs = normalized.get("kwargs", {})
        if kwargs is None:
            kwargs = {}
        if not isinstance(kwargs, dict):
            raise ValueError("simulation payload kwargs must be a JSON object")

        for field in ("sender", "contract", "function"):
            value = normalized.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"simulation payload missing {field}")

        return {
            "sender": normalized["sender"],
            "contract": normalized["contract"],
            "function": normalized["function"],
            "kwargs": kwargs,
        }

    @staticmethod
    def _payload_hash(payload: dict) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha3_256(encoded).hexdigest()

    def _make_environment(
        self, payload: dict | None = None, block_num: int = 1
    ) -> dict:
        block_meta = (
            (self.get_block_meta() or {}) if self.get_block_meta else {}
        )
        block_nanos = block_meta.get("nanos")
        if block_nanos is None:
            block_nanos = get_latest_block_nanos(
                self.client.raw_driver.storage_home
            )
        payload_hash = self._payload_hash(payload or {})
        block_hash = block_meta.get("hash")
        if block_hash is None:
            block_hash = hashlib.sha3_256(
                (
                    f"simulate:block:{block_meta.get('chain_id') or ''}:"
                    f"{block_meta.get('height', block_num)}:{int(block_nanos or 0)}"
                ).encode("utf-8")
            ).hexdigest()
        input_hash = hashlib.sha3_256(
            f"{int(block_nanos or 0)}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        now = Datetime._from_datetime(
            nanoseconds_to_utc_datetime(int(block_nanos or 0))
        )
        return {
            "block_hash": block_hash,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": input_hash,
            "now": now,
            "chain_id": block_meta.get("chain_id"),
        }
