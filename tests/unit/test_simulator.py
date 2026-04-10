import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from xian.simulator import QuerySimulationService, TransactionSimulator
from xian.utils.block import set_latest_block_nanos
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime


class SimulatorTests(unittest.TestCase):
    def test_make_environment_uses_latest_committed_block_time_when_idle(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)
            expected_nanos = 1_710_000_000_123_000_000
            set_latest_block_nanos(expected_nanos, storage_home)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
            simulator.get_block_meta = lambda: None

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 2024)
            self.assertEqual(environment["now"].month, 3)
            self.assertEqual(environment["now"].day, 9)
            self.assertEqual(environment["now"].microsecond, 123000)

    def test_make_environment_falls_back_to_epoch_without_chain_time(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
            simulator.get_block_meta = lambda: None

            environment = simulator._make_environment()

            self.assertEqual(environment["now"].year, 1970)
            self.assertEqual(environment["now"].month, 1)
            self.assertEqual(environment["now"].day, 1)

    def test_make_environment_is_deterministic_for_same_payload(self):
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
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
        with TemporaryDirectory() as tmpdir:
            storage_home = Path(tmpdir)

            simulator = object.__new__(TransactionSimulator)
            simulator.client = SimpleNamespace(
                raw_driver=SimpleNamespace(storage_home=storage_home)
            )
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

    def test_execute_normalizes_structured_result_for_abci(self):
        simulator = object.__new__(TransactionSimulator)
        simulator.client = SimpleNamespace(
            raw_driver=SimpleNamespace(
                pending_writes={},
                pending_reads={},
                pending_deltas={},
                transaction_reads={},
                transaction_read_prefixes=set(),
                transaction_writes={},
                log_events=[],
            ),
            get_var=lambda **kwargs: 20,
        )
        simulator.executor = SimpleNamespace(
            execute=lambda **kwargs: {
                "status_code": 0,
                "writes": {},
                "chi_used": 255,
                "result": {
                    "account": "alice",
                    "registered_at": Datetime(2026, 3, 27, 21, 57, 0),
                    "left_at": None,
                },
            }
        )
        simulator._make_environment = lambda payload, block_meta=None: {}

        result = simulator._execute(
            {
                "sender": "alice",
                "contract": "masternodes",
                "function": "get_validator",
                "kwargs": {"account": "alice"},
            }
        )

        self.assertEqual(result["status"], 0)
        self.assertEqual(
            result["result"],
            {
                "account": "alice",
                "registered_at": "2026-03-27 21:57:00",
                "left_at": None,
            },
        )

    def test_execute_normalizes_tuple_result_for_abci(self):
        simulator = object.__new__(TransactionSimulator)
        simulator.client = SimpleNamespace(
            raw_driver=SimpleNamespace(
                pending_writes={},
                pending_reads={},
                pending_deltas={},
                transaction_reads={},
                transaction_read_prefixes=set(),
                transaction_writes={},
                log_events=[],
            ),
            get_var=lambda **kwargs: 20,
        )
        simulator.executor = SimpleNamespace(
            execute=lambda **kwargs: {
                "status_code": 0,
                "writes": {},
                "chi_used": 853,
                "result": (
                    1,
                    ContractingDecimal("0.637954245540464949970792229477"),
                ),
            }
        )
        simulator._make_environment = lambda payload, block_meta=None: {}

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


class _TestQuerySimulationService(QuerySimulationService):
    def __init__(self, **kwargs):
        super().__init__(
            storage_home=Path("/tmp"),
            tracer_mode="python_line_v1",
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


class QuerySimulationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_disabled_result_when_simulation_is_off(self):
        service = _TestQuerySimulationService(enabled=False)
        payload = (
            '{"sender":"alice","contract":"currency","function":"balance_of",'
            '"kwargs":{"account":"alice"}}'
        ).encode("utf-8").hex()

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
            '{"sender":"alice","contract":"currency","function":"balance_of",'
            '"kwargs":{"account":"alice"}}'
        ).encode("utf-8").hex()

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
            '{"sender":"alice","contract":"currency","function":"balance_of",'
            '"kwargs":{"account":"alice"}}'
        ).encode("utf-8").hex()

        result = await service.simulate_encoded_transaction(payload)
        service.release_event.set()

        self.assertEqual(result["status"], 1)
        self.assertIn("timed out", result["result"])


if __name__ == "__main__":
    unittest.main()
