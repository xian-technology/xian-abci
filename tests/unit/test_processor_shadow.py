import unittest
from types import SimpleNamespace
from unittest import mock

from xian.processor import TxProcessor
from xian_runtime_types.time import Datetime


class ProcessorShadowExecutionTests(unittest.TestCase):
    def test_execute_tx_skips_native_shadow_for_source_only_submission(self):
        driver = SimpleNamespace(
            pending_writes={},
            pending_reads={},
            pending_deltas={},
            transaction_reads={},
            transaction_read_prefixes=set(),
            transaction_writes={},
            log_events=[],
        )
        driver.get_owner = lambda _contract: "sys"

        processor = object.__new__(TxProcessor)
        processor.client = SimpleNamespace(raw_driver=driver)
        processor.execution_runtime = SimpleNamespace(
            mode="xian_vm_v1",
            shadow_execution=True,
        )
        processor.profiler = None
        processor.trace_logging = False
        processor.executor = SimpleNamespace(
            execute=lambda **_kwargs: {
                "status_code": 0,
                "result": "ok",
                "writes": {},
                "events": [],
                "chi_used": 1,
            }
        )

        with (
            mock.patch(
                "xian.processor.prepare_contract_for_execution"
            ) as prepare,
            mock.patch("xian.processor.execute_native_contract") as native_execute,
        ):
            output = processor.execute_tx(
                transaction={
                    "payload": {
                        "sender": "sys",
                        "contract": "submission",
                        "function": "submit_contract",
                        "kwargs": {
                            "name": "con_probe",
                            "code": "@export\\ndef ping():\\n    return 'pong'\\n",
                        },
                        "chi_supplied": 1000,
                    }
                },
                chi_cost=20,
                environment={},
                metering=False,
            )

        self.assertEqual(output["status_code"], 0)
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "submission",
        )
        native_execute.assert_not_called()

    def test_execute_tx_runs_native_shadow_without_clobbering_python_state(self):
        driver = SimpleNamespace(
            pending_writes={},
            pending_reads={},
            pending_deltas={},
            transaction_reads={},
            transaction_read_prefixes=set(),
            transaction_writes={},
            log_events=[],
        )
        driver.get_owner = lambda _contract: "alice"

        processor = object.__new__(TxProcessor)
        processor.client = SimpleNamespace(raw_driver=driver)
        processor.execution_runtime = SimpleNamespace(
            mode="xian_vm_v1",
            shadow_execution=True,
        )
        processor.profiler = None
        processor.trace_logging = False

        def fake_execute(**_kwargs):
            driver.pending_writes = {"currency.balances:alice": 5}
            driver.transaction_writes = {"currency.balances:alice": 5}
            return {
                "status_code": 0,
                "result": "ok",
                "writes": {"currency.balances:alice": 5},
                "events": [],
                "chi_used": 1,
            }

        processor.executor = SimpleNamespace(execute=fake_execute)

        with (
            mock.patch(
                "xian.processor.prepare_contract_for_execution"
            ) as prepare,
            mock.patch(
                "xian.processor.execute_native_contract",
                return_value=SimpleNamespace(
                    status_code=0,
                    result="ok",
                    writes={"currency.balances:alice": 5},
                    events=[],
                ),
            ) as native_execute,
            mock.patch(
                "xian.processor.compare_execution_results",
                return_value={},
            ) as compare,
        ):
            output = processor.execute_tx(
                transaction={
                    "payload": {
                        "sender": "alice",
                        "contract": "currency",
                        "function": "transfer",
                        "kwargs": {"amount": 5, "to": "bob"},
                        "chi_supplied": 1000,
                    }
                },
                chi_cost=20,
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 7,
                    "block_hash": "abc123",
                    "chain_id": "xian-local",
                },
                metering=False,
            )

        self.assertEqual(output["status_code"], 0)
        self.assertEqual(driver.pending_writes, {"currency.balances:alice": 5})
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "currency",
        )
        native_execute.assert_called_once()
        compare.assert_called_once()

    def test_execute_tx_runs_native_authoritative_with_python_metering(self):
        driver = SimpleNamespace(
            pending_writes={},
            pending_reads={},
            pending_deltas={},
            transaction_reads={},
            transaction_read_prefixes=set(),
            transaction_writes={},
            log_events=[],
        )
        driver.get_owner = lambda _contract: "alice"

        processor = object.__new__(TxProcessor)
        processor.client = SimpleNamespace(raw_driver=driver)
        processor.execution_runtime = SimpleNamespace(
            mode="xian_vm_v1",
            native_authoritative=True,
            tracer_mode="python_line_v1",
        )
        processor.profiler = None
        processor.trace_logging = False
        processor.executor = SimpleNamespace(
            execute=lambda **_kwargs: {
                "status_code": 0,
                "result": "ok",
                "writes": {"currency.balances:alice": 5},
                "events": [],
                "chi_used": 11,
                "contract_costs": {"currency": 100},
            }
        )

        with (
            mock.patch(
                "xian.processor.prepare_contract_for_execution"
            ) as prepare,
            mock.patch(
                "xian.processor.execute_native_contract",
                return_value=SimpleNamespace(
                    status_code=0,
                    result="ok",
                    writes={"currency.balances:alice": 5},
                    events=[],
                ),
            ) as native_execute,
            mock.patch(
                "xian.processor.compare_execution_results",
                return_value={},
            ) as compare,
        ):
            output = processor.execute_tx(
                transaction={
                    "payload": {
                        "sender": "alice",
                        "contract": "currency",
                        "function": "transfer",
                        "kwargs": {"amount": 5, "to": "bob"},
                        "chi_supplied": 1000,
                    }
                },
                chi_cost=20,
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 7,
                    "block_hash": "abc123",
                    "chain_id": "xian-local",
                },
                metering=True,
            )

        self.assertEqual(output["status_code"], 0)
        self.assertEqual(output["chi_used"], 11)
        self.assertEqual(output["writes"], {"currency.balances:alice": 5})
        self.assertEqual(output["contract_costs"], {"currency": 100})
        self.assertEqual(driver.pending_writes, {})
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "currency",
        )
        native_execute.assert_called_once()
        compare.assert_called_once()


if __name__ == "__main__":
    unittest.main()
