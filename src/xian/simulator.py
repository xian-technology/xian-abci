from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from contracting.local import ContractingClient
from contracting.runtime_features import (
    RUNTIME_FEATURE_ZK,
    get_driver_runtime_features,
    runtime_feature_without_state,
    set_driver_runtime_features,
)
from contracting.storage.driver import Driver
from loguru import logger
from xian_runtime_types.encoding import decode_wire_numbers, loads_wire_json
from xian_runtime_types.time import Datetime
from xian_vm_core import HandedBack, TxRun, execute_tx

from xian.app_logging import build_log_fields
from xian.constants import Constants
from xian.execution_engine import (
    VmRuntime,
    execute_vm_transaction,
    vm_call_context,
)
from xian.execution_policy import EXECUTION_POLICY_KEY, VM_EXECUTION_MODE
from xian.fee_policy import chain_chi_rate
from xian.utils.block import (
    get_latest_block_nanos,
    nanoseconds_to_utc_datetime,
)
from xian.utils.encoding import normalize_contract_result, normalize_for_abci_json
from xian.utils.tx import canonical_transaction_size_bytes, format_dictionary

# The chi a simulation may spend when the caller sets no cap.
DEFAULT_SIMULATION_CHI = 1_000_000
NONCE_FILENAME = Constants.NONCE_FILENAME


def _payload_nonce(payload: dict) -> int | None:
    """The nonce the transaction will be signed with, when the request
    gives it: it sizes the estimated envelope."""
    nonce = payload.get("nonce")
    if nonce is not None and (type(nonce) is not int or nonce < 0):
        raise ValueError("simulation payload nonce must be a non-negative integer")
    return nonce


def _simulation_error_result(
    *,
    payload: dict | None,
    message: str,
) -> dict:
    return {
        "payload": payload,
        "status": 1,
        "state": [],
        "chi_used": 0,
        "result": message,
    }


def _estimated_transaction_size(
    payload: dict, *, chain_id: str | None, nonce: int, chi_supplied: int
) -> int:
    """The canonical size of the signed transaction a simulation estimates:
    its arguments, the sender's nonce and ``chi_supplied`` in the real
    envelope (the signature's length is fixed)."""
    call = {key: value for key, value in payload.items() if key != "nonce"}
    return canonical_transaction_size_bytes(
        {
            "metadata": {"signature": "0" * 128},
            "payload": {
                **call,
                "chain_id": chain_id or "",
                "nonce": nonce,
                "chi_supplied": chi_supplied,
            },
        }
    )


Estimate = TypeVar("Estimate")


def estimate_to_fixpoint(
    run: Callable[[int], Estimate], chi_used: Callable[[Estimate], int], *, rounds: int = 4
) -> Estimate:
    """The estimate of a transaction that supplies exactly the chi it uses.

    ``run(chi_supplied)`` simulates with ``chi_supplied`` in the envelope; its
    digits are bytes the transaction pays for, so the estimate is re-run with
    its own chi until the digit count no longer changes (the chi used grows
    with the digits, so this ends at the smallest self-consistent estimate).
    Execution counts the base chi against ``chi_supplied``, so supplying the
    estimate succeeds and one chi less runs out.
    """
    supplied = 1
    estimate = run(supplied)
    for _ in range(rounds):
        used = chi_used(estimate)
        if len(str(used)) == len(str(supplied)):
            break
        supplied = used
        estimate = run(supplied)
    return estimate


def _simulation_result(payload: dict, *, status: int, writes, events, chi_used, result) -> dict:
    """The ``simulate_tx`` response of a finished simulation."""
    response = {
        "payload": payload,
        "status": status,
        "state": [{"key": key, "value": value} for key, value in writes.items()],
        "events": events,
        "chi_used": chi_used,
        "result": normalize_contract_result(result),
    }
    return normalize_for_abci_json(format_dictionary(response))


class TransactionSimulator:
    """A readonly dry run of one call against node state.

    The call runs in Rust with the code a block runs (``xian_vm_core.
    execute_tx``) over a fork of the pending block state, which is dropped
    afterwards: metered, with the chain's fee policy, but without reserving
    the fee budget (a dry run reports the chi used, so a small balance still
    gets an estimate). The few calls the Rust executor leaves to Python run
    on the Python per-transaction path instead, over the same state.
    """

    def __init__(
        self,
        *,
        client: ContractingClient,
        get_block_meta=None,
        execution_runtime: VmRuntime | None = None,
        chain_id: str | None = None,
        charge_fees: bool = True,
    ):
        self.client = client
        self.get_block_meta = get_block_meta or (lambda: None)
        self.execution_runtime = execution_runtime or VmRuntime()
        self.chain_id = chain_id
        self.charge_fees = bool(charge_fees)

    def simulate(
        self,
        payload: dict,
        *,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        """The simulation of ``payload`` against the client's pending state."""
        normalized_payload = self._normalize_payload(payload)
        nonce = self.envelope_nonce(normalized_payload)
        _environment, overlay = self.client.raw_driver.committed_storage()
        outcome = self.simulate_in_rust(
            normalized_payload, block_meta=block_meta, max_chi=max_chi, pending=overlay, nonce=nonce
        )
        if isinstance(outcome, HandedBack):
            return self.simulate_on_python_path(
                normalized_payload, block_meta=block_meta, max_chi=max_chi, nonce=nonce
            )
        return outcome

    def envelope_nonce(self, payload: dict) -> int:
        """The nonce that sizes the estimated envelope: the request's, else
        the sender's next committed nonce (a client with transactions still
        in the mempool passes its nonce). Reads the driver: call it where
        node state may be read (under the node state lock)."""
        nonce = payload.get("nonce")
        if nonce is not None:
            return nonce
        committed = self.client.raw_driver.get(f"{NONCE_FILENAME}.{payload['sender']}:")
        return committed + 1 if type(committed) is int else 0

    def simulate_encoded_transaction(
        self,
        raw_payload_hex: str,
        *,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        decoded = loads_wire_json(bytes.fromhex(raw_payload_hex))
        return self.simulate(
            decoded,
            block_meta=block_meta,
            max_chi=max_chi,
        )

    def simulate_in_rust(
        self,
        payload: dict,
        *,
        pending,
        nonce: int,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict | HandedBack:
        """The simulation of the normalized ``payload`` over a fork of
        ``pending`` (a ``BlockState``), or ``HandedBack`` for the Python
        path. Thread-safe: it reads only ``pending`` and LMDB."""
        return self._guarded(
            payload,
            lambda: self._execute_in_rust(
                payload, pending=pending, nonce=nonce, block_meta=block_meta, max_chi=max_chi
            ),
        )

    def simulate_on_python_path(
        self,
        payload: dict,
        *,
        nonce: int,
        block_meta: dict | None = None,
        max_chi: int | None = None,
    ) -> dict:
        """The simulation of the normalized ``payload`` on the Python
        per-transaction path; the driver's pending state is restored after."""
        return self._guarded(
            payload,
            lambda: self._execute_on_python_path(
                payload, nonce=nonce, block_meta=block_meta, max_chi=max_chi
            ),
        )

    @staticmethod
    def _guarded(payload: dict, run) -> dict | HandedBack:
        try:
            return run()
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=payload,
                    extra={"error_type": type(exc).__name__},
                )
            ).error("Simulation failed")
            return _simulation_error_result(
                payload=payload,
                message=f"Simulation error: {exc}",
            )

    @staticmethod
    def _budget(max_chi: int | None) -> int:
        return max(int(max_chi or DEFAULT_SIMULATION_CHI), 1)

    def _transaction_size(
        self, payload: dict, environment: dict, *, nonce: int, chi_supplied: int
    ) -> int:
        """The transaction size the simulation is charged for."""
        return _estimated_transaction_size(
            payload, chain_id=environment.get("chain_id"), nonce=nonce, chi_supplied=chi_supplied
        )

    def _execute_in_rust(
        self,
        payload: dict,
        *,
        pending,
        nonce: int,
        block_meta: dict | None,
        max_chi: int | None,
    ) -> dict | HandedBack:
        driver = self.client.raw_driver
        storage, _overlay = driver.committed_storage()
        kwargs = decode_wire_numbers(payload.get("kwargs", {}))
        environment = self._make_environment(payload, block_meta=block_meta)
        zk_without_state = runtime_feature_without_state(driver, RUNTIME_FEATURE_ZK)
        run = estimate_to_fixpoint(
            lambda chi_supplied: self._run_in_rust(
                storage,
                pending,
                payload,
                kwargs,
                environment,
                max_chi=max_chi,
                size=self._transaction_size(
                    payload, environment, nonce=nonce, chi_supplied=chi_supplied
                ),
                zk_without_state=zk_without_state,
            ),
            lambda run: 0 if isinstance(run, HandedBack) else run.chi_used,
        )
        if isinstance(run, HandedBack):
            return run
        return self._rust_result(payload, run)

    def _run_in_rust(
        self, storage, pending, payload, kwargs, environment, *, max_chi, size, zk_without_state
    ):
        return execute_tx(
            storage,
            pending.snapshot(),
            sender=payload["sender"],
            contract_name=payload["contract"],
            function_name=payload["function"],
            kwargs=kwargs,
            context=vm_call_context(
                sender=payload["sender"],
                contract_name=payload["contract"],
                function_name=payload["function"],
                kwargs=kwargs,
                environment=environment,
            ),
            meter=True,
            charge_fees=self.charge_fees,
            chi_budget=self._budget(max_chi),
            transaction_size_bytes=size,
            protected_keys=(EXECUTION_POLICY_KEY,),
            zk_enabled_without_state=zk_without_state,
            estimate=True,
        )

    @staticmethod
    def _rust_result(payload: dict, run: TxRun) -> dict:
        return _simulation_result(
            payload,
            status=run.status_code,
            writes=run.writes,
            events=run.events,
            chi_used=run.chi_used,
            result=run.result,
        )

    def _execute_on_python_path(
        self,
        payload: dict,
        *,
        nonce: int,
        block_meta: dict | None,
        max_chi: int | None,
    ) -> dict:
        state_snapshot = self.client.raw_driver.snapshot_state()
        try:
            # Read inside the snapshot so the estimate leaves no tracked reads.
            chi_cost = chain_chi_rate(self.client) if self.charge_fees else None
            converted_kwargs = decode_wire_numbers(payload.get("kwargs", {}))
            environment = self._make_environment(payload, block_meta=block_meta)
            return self._execute_vm_simulation(
                payload=payload,
                kwargs=converted_kwargs,
                environment=environment,
                max_chi=max_chi,
                chi_cost=chi_cost,
                nonce=nonce,
            )
        finally:
            self.client.raw_driver.restore_state(state_snapshot)

    def _execute_vm_simulation(
        self,
        *,
        payload: dict,
        kwargs: dict,
        environment: dict,
        max_chi: int | None,
        chi_cost: int,
        nonce: int,
    ) -> dict:
        def run(chi_supplied: int):
            return execute_vm_transaction(
                self.execution_runtime,
                self.client.raw_driver,
                sender=payload["sender"],
                contract_name=payload["contract"],
                function_name=payload["function"],
                kwargs=kwargs,
                environment=environment,
                chi_budget=self._budget(max_chi),
                transaction_size_bytes=self._transaction_size(
                    payload, environment, nonce=nonce, chi_supplied=chi_supplied
                ),
                chi_cost=chi_cost,
                meter=True,
                apply_metering_on_success_only=False,
                apply_metering_writes=self.charge_fees,
                # This is a cost estimate, not a transaction with a submitted budget.
                reserve_fee_budget=False,
            )

        outcome = estimate_to_fixpoint(run, lambda outcome: outcome.chi_used)
        return _simulation_result(
            payload,
            status=outcome.output.status_code,
            writes=outcome.writes,
            events=outcome.output.events,
            chi_used=outcome.chi_used,
            result=outcome.output.result,
        )

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

        simulated = {
            "sender": normalized["sender"],
            "contract": normalized["contract"],
            "function": normalized["function"],
            "kwargs": kwargs,
        }
        nonce = _payload_nonce(normalized)
        if nonce is not None:
            simulated["nonce"] = nonce
        return simulated

    @staticmethod
    def _payload_hash(payload: dict) -> str:
        # The call, without the nonce that only sizes the envelope.
        call = {key: value for key, value in payload.items() if key != "nonce"}
        encoded = json.dumps(
            call,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha3_256(encoded).hexdigest()

    def _make_environment(
        self,
        payload: dict | None = None,
        block_num: int = 1,
        *,
        block_meta: dict | None = None,
    ) -> dict:
        block_meta = block_meta or ((self.get_block_meta() or {}) if self.get_block_meta else {})
        block_nanos = block_meta.get("nanos")
        if block_nanos is None:
            block_nanos = get_latest_block_nanos(self.client.raw_driver.storage_home)
        payload_hash = self._payload_hash(payload or {})
        chain_id = block_meta.get("chain_id") or self.chain_id
        block_hash = block_meta.get("hash")
        if block_hash is None:
            block_hash = hashlib.sha3_256(
                (
                    f"simulate:block:{chain_id or ''}:"
                    f"{block_meta.get('height', block_num)}:{int(block_nanos or 0)}"
                ).encode("utf-8")
            ).hexdigest()
        input_hash = hashlib.sha3_256(
            f"{int(block_nanos or 0)}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        now = Datetime._from_datetime(nanoseconds_to_utc_datetime(int(block_nanos or 0)))
        execution_runtime = getattr(self, "execution_runtime", None)
        return {
            "block_hash": block_hash,
            "block_num": block_meta.get("height", block_num),
            "__input_hash": input_hash,
            "__xian_execution_mode__": (getattr(execution_runtime, "mode", None) or "xian_vm_v2"),
            "now": now,
            "chain_id": chain_id,
        }


class QuerySimulationService:
    """The node's ``simulate_tx`` queries, run in-process.

    ``prepare_encoded_transaction`` forks the pending block state while the
    caller holds the node state lock; the simulation then runs in Rust on a
    worker thread with the GIL released, so consensus keeps going. Chi bounds
    the work; the timeout bounds how long a query waits for its answer.
    """

    def __init__(
        self,
        *,
        client: ContractingClient,
        execution_runtime: VmRuntime | None = None,
        get_block_meta=None,
        chain_id: str | None = None,
        enabled: bool = True,
        max_concurrency: int = 2,
        timeout_ms: int = 3000,
        max_chi: int = DEFAULT_SIMULATION_CHI,
        charge_fees: bool = True,
    ) -> None:
        self.client = client
        self.execution_runtime = execution_runtime or VmRuntime()
        self.get_block_meta = get_block_meta or (lambda: None)
        self.chain_id = chain_id
        self.enabled = enabled
        self.max_concurrency = max(int(max_concurrency), 1)
        self.timeout_ms = max(int(timeout_ms), 1)
        self.max_chi = max(int(max_chi), 1)
        self.charge_fees = bool(charge_fees)
        self._active_requests = 0

    async def simulate_encoded_transaction(self, raw_payload_hex: str) -> dict:
        prepared = self.prepare_encoded_transaction(raw_payload_hex)
        if isinstance(prepared, dict):
            return prepared
        return await prepared.run()

    def prepare_encoded_transaction(self, raw_payload_hex: str) -> "PreparedSimulation | dict":
        """Fork node state for a simulation without running it.

        Callers holding the node state lock can release it before awaiting
        ``PreparedSimulation.run``.
        """
        try:
            decoded_payload = loads_wire_json(bytes.fromhex(raw_payload_hex))
            normalized_payload = TransactionSimulator._normalize_payload(decoded_payload)
        except Exception as exc:
            return _simulation_error_result(
                payload=None,
                message=f"Simulation error: {exc}",
            )

        if not self.enabled:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=normalized_payload,
                )
            ).info("Rejected readonly simulation because it is disabled")
            return _simulation_error_result(
                payload=normalized_payload,
                message="Simulation is disabled on this node",
            )

        _environment, overlay = self.client.raw_driver.committed_storage()
        simulator = self._simulator(self.client)
        return PreparedSimulation(
            service=self,
            simulator=simulator,
            payload=normalized_payload,
            nonce=simulator.envelope_nonce(normalized_payload),
            block_meta=self.get_block_meta() or {},
            max_chi=self.max_chi,
            pending=overlay.snapshot(),
        )

    def _simulator(self, client: ContractingClient) -> TransactionSimulator:
        return TransactionSimulator(
            client=client,
            execution_runtime=self.execution_runtime,
            chain_id=self.chain_id,
            charge_fees=self.charge_fees,
        )

    def _acquire(self, payload: dict) -> bool:
        if self._active_requests >= self.max_concurrency:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=payload,
                    extra={
                        "active_requests": self._active_requests,
                        "simulation_max_concurrency": (self.max_concurrency),
                    },
                )
            ).warning("Rejected readonly simulation because capacity is exhausted")
            return False
        self._active_requests += 1
        return True

    def _release(self, _finished=None) -> None:
        self._active_requests -= 1

    async def _run_prepared(self, prepared: "PreparedSimulation") -> dict:
        if not self._acquire(prepared.payload):
            return _simulation_error_result(
                payload=prepared.payload,
                message=("Simulation capacity exceeded on this node; retry later"),
            )
        # The slot stays taken until the run finishes, even after a timeout.
        running = asyncio.ensure_future(asyncio.to_thread(prepared.run_in_rust))
        running.add_done_callback(self._release)
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(running), timeout=self.timeout_ms / 1000
            )
        except TimeoutError:
            logger.bind(
                **build_log_fields(
                    stage="simulate_tx",
                    payload=prepared.payload,
                    extra={"simulation_timeout_ms": self.timeout_ms},
                )
            ).warning("Readonly simulation timed out")
            return _simulation_error_result(
                payload=prepared.payload,
                message=(f"Simulation timed out on this node after {self.timeout_ms} ms"),
            )
        if isinstance(outcome, HandedBack):
            # The Python path shares process state with consensus execution,
            # so it runs here, on the event loop, never on a worker thread.
            return prepared.run_on_python_path()
        return outcome

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class PreparedSimulation:
    service: QuerySimulationService
    simulator: TransactionSimulator
    payload: dict
    # The nonce that sizes the envelope, read while preparing (node state).
    nonce: int
    block_meta: dict
    max_chi: int
    # The fork of the pending block state the simulation reads.
    pending: object

    async def run(self) -> dict:
        return await self.service._run_prepared(self)

    def run_in_rust(self) -> dict | HandedBack:
        return self.simulator.simulate_in_rust(
            self.payload,
            pending=self.pending,
            nonce=self.nonce,
            block_meta=self.block_meta,
            max_chi=self.max_chi,
        )

    def run_on_python_path(self) -> dict:
        """The simulation on the Python path, over a driver of its own that
        holds the forked pending state and the node's runtime features."""
        live = self.simulator.client.raw_driver
        driver = Driver(vm_profile=VM_EXECUTION_MODE, storage_home=live.storage_home)
        try:
            driver.restore_state(
                {"pending_writes": self.pending.items(), "transaction_writes": {}, "log_events": []}
            )
            set_driver_runtime_features(driver, get_driver_runtime_features(live))
            client = ContractingClient(
                vm_profile=VM_EXECUTION_MODE, driver=driver, submission_filename=None
            )
            return self.service._simulator(client).simulate_on_python_path(
                self.payload, nonce=self.nonce, block_meta=self.block_meta, max_chi=self.max_chi
            )
        finally:
            driver.close()
