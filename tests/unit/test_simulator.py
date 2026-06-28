import asyncio
import decimal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from contracting.local import ContractingClient
from contracting.storage.driver import Driver
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

from xian import simulator_ipc
from xian.execution_engine import build_vm_runtime
from xian.simulator import QuerySimulationService, TransactionSimulator
from xian.utils.block import set_latest_block_nanos


class _FakeDriver(SimpleNamespace):
    # Borrow the real state snapshot implementation so the fake stays in
    # sync with the Driver interface the simulator relies on.
    snapshot_state = Driver.snapshot_state
    restore_state = Driver.restore_state


def _driver(storage_home: Path | None = None):
    driver = _FakeDriver(
        storage_home=storage_home or Path("/tmp"),
        pending_writes={},
        pending_reads={},
        pending_deltas={},
        transaction_reads={},
        transaction_read_prefixes=set(),
        transaction_writes={},
        log_events=[],
    )
    return driver


def _simulator(driver=None, *, chain_id: str | None = None):
    simulator = object.__new__(TransactionSimulator)
    simulator.client = SimpleNamespace(
        raw_driver=driver or _driver(),
        get_var=lambda **kwargs: 20,
    )
    simulator.execution_runtime = SimpleNamespace(mode="xian_vm_v1")
    simulator.get_block_meta = lambda: None
    simulator.chain_id = chain_id
    return simulator


def _native_result(result, *, status_code=0, chi_used=255):
    return SimpleNamespace(
        output=SimpleNamespace(
            status_code=status_code,
            result=result,
        ),
        writes={},
        chi_used=chi_used,
    )


class SimulatorIpcTests(unittest.TestCase):
    def test_round_trips_supported_runtime_values(self):
        payload = {
            "decimal": decimal.Decimal("1.2300"),
            "contracting_decimal": ContractingDecimal("4.5600"),
            "datetime": Datetime(2025, 1, 2, 3, 4, 5, 123456),
            "set": {"prefix.b", "prefix.a"},
            "tuple": ("left", "right"),
            "bytes": b"abc",
        }

        decoded = simulator_ipc.loads(simulator_ipc.dumps(payload))

        self.assertEqual(decoded["decimal"], decimal.Decimal("1.2300"))
        self.assertEqual(str(decoded["contracting_decimal"]), "4.56")
        self.assertEqual(str(decoded["datetime"]), "2025-01-02 03:04:05.123456")
        self.assertEqual(decoded["set"], {"prefix.b", "prefix.a"})
        self.assertEqual(decoded["tuple"], ("left", "right"))
        self.assertEqual(decoded["bytes"], b"abc")


class SimulatorTests(unittest.TestCase):
    def test_make_environment_uses_latest_committed_block_time_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)
            expected_nanos = 1_710_000_000_123_000_000
            set_latest_block_nanos(expected_nanos, storage_home)

            simulator = _simulator(_driver(storage_home))

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 2024)
            self.assertEqual(environment["now"].month, 3)
            self.assertEqual(environment["now"].day, 9)
            self.assertEqual(environment["now"].microsecond, 123000)

    def test_make_environment_falls_back_to_epoch_without_chain_time(self):
        with TemporaryDirectory() as tmpdir:
            simulator = _simulator(_driver(Path(tmpdir)))

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 1970)
            self.assertEqual(environment["now"].month, 1)
            self.assertEqual(environment["now"].day, 1)

    def test_make_environment_uses_configured_chain_id_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            simulator = _simulator(
                _driver(Path(tmpdir)),
                chain_id="xian-local",
            )

            environment = simulator._make_environment()

            self.assertEqual(environment["chain_id"], "xian-local")

    def test_make_environment_is_deterministic_for_same_payload(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }
        payload = {
            "sender": "alice",
            "contract": "con_game",
            "function": "roll",
            "kwargs": {"sides": 6},
        }

        first = simulator._make_environment(payload)
        second = simulator._make_environment(payload)

        self.assertEqual(first["block_hash"], second["block_hash"])
        self.assertEqual(first["__input_hash"], second["__input_hash"])

    def test_make_environment_changes_input_hash_for_different_payloads(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }

        left = simulator._make_environment(
            {
                "sender": "alice",
                "contract": "con_game",
                "function": "roll",
                "kwargs": {"sides": 6},
            }
        )
        right = simulator._make_environment(
            {
                "sender": "alice",
                "contract": "con_game",
                "function": "roll",
                "kwargs": {"sides": 20},
            }
        )

        self.assertEqual(left["block_hash"], right["block_hash"])
        self.assertNotEqual(left["__input_hash"], right["__input_hash"])

    def test_make_environment_exposes_internal_execution_mode(self):
        simulator = _simulator()
        simulator.get_block_meta = lambda: {
            "height": 7,
            "nanos": 1_710_000_000_123_000_000,
            "chain_id": "xian-local",
        }

        environment = simulator._make_environment({"contract": "currency"})

        self.assertEqual(environment["__xian_execution_mode__"], "xian_vm_v1")

    def test_execute_normalizes_structured_result_for_abci(self):
        simulator = _simulator()
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract"),
            mock.patch(
                "xian.simulator.execute_vm_transaction",
                return_value=_native_result(
                    {
                        "account": "alice",
                        "left_at": None,
                    }
                ),
            ),
        ):
            result = simulator._execute(
                {
                    "sender": "alice",
                    "contract": "validators",
                    "function": "get_validator",
                    "kwargs": {"account": "alice"},
                }
            )

        self.assertEqual(result["status"], 0)
        self.assertEqual(
            result["result"],
            {
                "account": "alice",
                "left_at": None,
            },
        )

    def test_execute_normalizes_tuple_result_for_abci(self):
        simulator = _simulator()
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract"),
            mock.patch(
                "xian.simulator.execute_vm_transaction",
                return_value=_native_result(
                    (
                        1,
                        ContractingDecimal("0.637954245540464949970792229477"),
                    ),
                    chi_used=853,
                ),
            ),
        ):
            result = simulator._execute(
                {
                    "sender": "alice",
                    "contract": "con_ixhelper_demo",
                    "function": "sell",
                    "kwargs": {"amount": 1},
                }
            )

        self.assertEqual(result["status"], 0)
        self.assertEqual(
            result["result"],
            [1, "0.637954245540464949970792229477"],
        )

    def test_execute_normalizes_exception_result_for_abci(self):
        simulator = _simulator()
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract"),
            mock.patch(
                "xian.simulator.execute_vm_transaction",
                return_value=_native_result(
                    AssertionError("boom"),
                    status_code=1,
                    chi_used=0,
                ),
            ),
        ):
            result = simulator._execute(
                {
                    "sender": "alice",
                    "contract": "con_demo",
                    "function": "f",
                    "kwargs": {},
                }
            )

        self.assertEqual(result["status"], 1)
        self.assertEqual(result["result"], "AssertionError('boom')")

    def test_execute_preflights_contract_for_vm(self):
        simulator = _simulator()
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract") as prepare,
            mock.patch(
                "xian.simulator.execute_vm_transaction",
                return_value=_native_result("ok", chi_used=1),
            ),
        ):
            result = simulator._execute(
                {
                    "sender": "alice",
                    "contract": "currency",
                    "function": "balance_of",
                    "kwargs": {"account": "alice"},
                }
            )

        self.assertEqual(result["status"], 0)
        prepare.assert_called_once_with(
            simulator.execution_runtime,
            simulator.client.raw_driver,
            "currency",
        )

    def test_execute_honors_free_metered_simulation_fee_policy(self):
        simulator = _simulator()
        simulator.charge_fees = False
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract"),
            mock.patch(
                "xian.simulator.execute_vm_transaction",
                return_value=_native_result("ok", chi_used=1),
            ) as native_execute,
        ):
            result = simulator._execute(
                {
                    "sender": "alice",
                    "contract": "currency",
                    "function": "balance_of",
                    "kwargs": {"account": "alice"},
                }
            )

        self.assertEqual(result["status"], 0)
        self.assertFalse(native_execute.call_args.kwargs["apply_metering_writes"])

    def test_execute_rejects_submission_without_source_for_xian_vm(self):
        simulator = _simulator()
        simulator._make_environment = lambda payload, block_meta=None: {}

        with (
            mock.patch("xian.simulator.prepare_vm_contract") as prepare,
            mock.patch("xian.simulator.execute_vm_transaction") as native_execute,
        ):
            result = simulator._execute(
                {
                    "sender": "sys",
                    "contract": "submission",
                    "function": "submit_contract",
                    "kwargs": {
                        "name": "con_probe",
                    },
                }
            )

        self.assertEqual(result["status"], 1)
        self.assertIn("requires source code", result["result"])
        prepare.assert_called_once_with(
            simulator.execution_runtime,
            simulator.client.raw_driver,
            "submission",
        )
        native_execute.assert_not_called()

    def test_execute_simulates_source_only_submission_and_reports_chi(self):
        source = (
            "v = Variable()\n\n"
            "@construct\n"
            "def seed(value: int = 3):\n"
            "    v.set(value)\n\n"
            "@export\n"
            "def ping():\n"
            "    return v.get()\n"
        )
        client = ContractingClient(environment={"chain_id": "test-chain"})
        client.flush()
        try:
            simulator = TransactionSimulator(
                client=client,
                execution_runtime=build_vm_runtime(),
                chain_id="test-chain",
            )

            result = simulator.simulate(
                {
                    "sender": "sys",
                    "contract": "submission",
                    "function": "submit_contract",
                    "kwargs": {
                        "name": "con_simulated_deploy_probe",
                        "code": source,
                        "constructor_args": {"value": 9},
                    },
                }
            )

            self.assertEqual(result["status"], 0)
            self.assertGreater(result["chi_used"], 0)
            writes = {item["key"]: item["value"] for item in result["state"]}
            self.assertEqual(writes["con_simulated_deploy_probe.v"], 9)
            self.assertIn("con_simulated_deploy_probe.__source__", writes)
            self.assertIn("con_simulated_deploy_probe.__xian_ir_v1__", writes)
            self.assertIsNone(client.raw_driver.get_contract_source("con_simulated_deploy_probe"))
        finally:
            client.flush()


class _TestQuerySimulationService(QuerySimulationService):
    def __init__(self, **kwargs):
        super().__init__(
            storage_home=Path("/tmp"),
            **kwargs,
        )
        self.release_event = asyncio.Event()
        self.response = {
            "payload": {
                "sender": "alice",
                "contract": "currency",
                "function": "balance_of",
                "kwargs": {"account": "alice"},
            },
            "status": 0,
            "state": [],
            "chi_used": 42,
            "result": "100",
        }
        self.process = _DummyProcess()

    async def _start_task_process(self, task: dict):
        self.process = _DummyProcess()
        return self.process

    async def _wait_for_task_result(self, process, task: dict) -> dict:
        await self.release_event.wait()
        return self.response


class _DummyProcess:
    def __init__(self) -> None:
        self.returncode = None

    async def wait(self) -> None:
        return None

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class _CapturingProcess:
    returncode = 0

    def __init__(self) -> None:
        self.stdin = None

    async def communicate(self, stdin):
        self.stdin = stdin
        return simulator_ipc.dumps({"ok": True}), b""


class QuerySimulationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_disabled_result_when_simulation_is_off(self):
        service = _TestQuerySimulationService(enabled=False)
        payload = (
            (
                '{"sender":"alice","contract":"currency","function":"balance_of",'
                '"kwargs":{"account":"alice"}}'
            )
            .encode("utf-8")
            .hex()
        )

        result = await service.simulate_encoded_transaction(payload)

        self.assertEqual(result["status"], 1)
        self.assertIn("disabled", result["result"])

    async def test_rejects_requests_above_capacity(self):
        service = _TestQuerySimulationService(
            enabled=True,
            max_concurrency=1,
            timeout_ms=1000,
        )
        payload = (
            (
                '{"sender":"alice","contract":"currency","function":"balance_of",'
                '"kwargs":{"account":"alice"}}'
            )
            .encode("utf-8")
            .hex()
        )

        first = asyncio.create_task(service.simulate_encoded_transaction(payload))
        await asyncio.sleep(0)
        second = await service.simulate_encoded_transaction(payload)
        service.release_event.set()
        first_result = await first

        self.assertEqual(first_result["status"], 0)
        self.assertEqual(second["status"], 1)
        self.assertIn("capacity exceeded", second["result"])

    async def test_times_out_long_running_simulations(self):
        service = _TestQuerySimulationService(
            enabled=True,
            max_concurrency=1,
            timeout_ms=1,
        )
        payload = (
            (
                '{"sender":"alice","contract":"currency","function":"balance_of",'
                '"kwargs":{"account":"alice"}}'
            )
            .encode("utf-8")
            .hex()
        )

        result = await service.simulate_encoded_transaction(payload)
        service.release_event.set()

        self.assertEqual(result["status"], 1)
        self.assertIn("timed out", result["result"])

    async def test_worker_ipc_sends_json_bytes(self):
        service = QuerySimulationService(storage_home=Path("/tmp"))
        process = _CapturingProcess()

        result = await service._wait_for_task_result(
            process,
            {"driver_state": {"transaction_read_prefixes": {"prefix.a"}}},
        )

        self.assertEqual(result, {"ok": True})
        self.assertIsNotNone(process.stdin)
        self.assertTrue(process.stdin.startswith(b"{"))
        self.assertNotEqual(process.stdin[:1], b"\x80")


if __name__ == "__main__":
    unittest.main()
