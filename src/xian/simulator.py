from __future__ import annotations

import json
import secrets
from copy import deepcopy
from datetime import datetime

from contracting.client import ContractingClient
from contracting.execution.executor import Executor
from contracting.stdlib.bridge.time import Datetime
from contracting.storage.encoder import convert_dict, safe_repr
from loguru import logger

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
                environment=self._make_environment(),
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

    def _make_environment(self, block_num: int = 1) -> dict:
        block_meta = (
            (self.get_block_meta() or {}) if self.get_block_meta else {}
        )
        salt = secrets.token_hex(32)
        return {
            "block_hash": salt,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": salt,
            "now": Datetime._from_datetime(datetime.now()),
            "AUXILIARY_SALT": salt,
            "chain_id": block_meta.get("chain_id"),
        }
