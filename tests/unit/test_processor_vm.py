import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from xian_runtime_types.time import Datetime

from xian.processor import TxProcessor


def _driver():
    driver = SimpleNamespace(
        transaction_writes={},
        log_events=[],
    )
    driver.get_owner = lambda _contract: "alice"
    driver.clear_transaction_reads = lambda: None
    driver.clear_transaction_writes = lambda: None
    return driver


def _processor(driver):
    processor = object.__new__(TxProcessor)
    processor.client = SimpleNamespace(raw_driver=driver)
    processor.execution_runtime = SimpleNamespace(mode="xian_vm_v2")
    processor.profiler = None
    processor.trace_logging = False
    processor.currency_contract = "currency"
    processor.balances_hash = "balances"
    return processor


def _transaction(**payload_overrides):
    payload = {
        "sender": "alice",
        "contract": "currency",
        "function": "transfer",
        "kwargs": {"amount": 1, "to": "bob"},
        "chi_supplied": 1000,
    }
    payload.update(payload_overrides)
    return {"payload": payload}


class ProcessorVmExecutionTests(unittest.TestCase):
    def test_nested_runtime_wrappers_are_converted_before_vm_execution(self):
        processor = _processor(_driver())
        transaction = _transaction(
            contract="con_probe",
            function="run",
            kwargs={"data": [[{"__big_int__": str(2**80)}, True]]},
        )
        with (
            mock.patch.object(processor, "_execute_vm_tx", return_value={}) as execute,
        ):
            processor.execute_tx(transaction, chi_cost=20)
        self.assertEqual(execute.call_args.kwargs["kwargs"], {"data": [[2**80, True]]})

    def test_x402_nested_wire_tags_reach_vm_without_reencoding_the_envelope(self):
        from copy import deepcopy

        from xian_runtime_types.decimal import ContractingDecimal

        processor = _processor(_driver())
        transaction = _transaction(
            contract="con_x402",
            function="settle",
            kwargs={
                "amount": {"__fixed__": "0.001"},
                "nested": [{"fee": {"__fixed__": "2"}}, {"__bytes__": "00ff"}],
            },
        )
        original = deepcopy(transaction)
        outcome = SimpleNamespace(
            output=SimpleNamespace(status_code=0, result="settled", events=[]),
            writes={},
            chi_used=1,
            contract_costs={},
        )
        with (
            mock.patch("xian.processor.execute_vm_transaction", return_value=outcome) as execute,
        ):
            result = processor.execute_tx(
                transaction, chi_cost=20, metering=True, charge_fees=False
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status_code"], 0)
        self.assertEqual(
            execute.call_args.kwargs["kwargs"],
            {
                "amount": ContractingDecimal("0.001"),
                "nested": [{"fee": ContractingDecimal("2")}, b"\x00\xff"],
            },
        )
        self.assertGreater(execute.call_args.kwargs["transaction_size_bytes"], 0)
        self.assertEqual(transaction, original)

    def test_execute_tx_uses_fresh_environment_when_omitted(self):
        processor = _processor(_driver())
        observed_environments = []

        def fake_execute_authoritative(*_args, **kwargs):
            observed_environments.append(dict(kwargs["environment"]))
            kwargs["environment"]["touched"] = True
            return SimpleNamespace(
                output=SimpleNamespace(
                    status_code=0,
                    result="ok",
                    events=[],
                ),
                writes={},
                chi_used=1,
                contract_costs={},
            )

        with (
            mock.patch(
                "xian.processor.execute_vm_transaction",
                side_effect=fake_execute_authoritative,
            ),
        ):
            first = processor.execute_tx(_transaction(), chi_cost=20)
            second = processor.execute_tx(_transaction(), chi_cost=20)

        self.assertEqual(first["status_code"], 0)
        self.assertEqual(second["status_code"], 0)
        self.assertEqual(observed_environments[0], {})
        self.assertEqual(observed_environments[1], {})

    def test_get_environment_exposes_internal_execution_mode(self):
        processor = object.__new__(TxProcessor)
        processor.execution_runtime = SimpleNamespace(mode="xian_vm_v2")
        processor.get_timestamp_hash_from_tx = lambda nanos, signature: f"{nanos}:{signature}"
        processor.get_now_from_nanos = lambda nanos: f"now:{nanos}"

        environment = processor.get_environment(
            {
                "b_meta": {
                    "hash": "abc123",
                    "height": 7,
                    "nanos": 123,
                    "chain_id": "xian-local",
                },
                "metadata": {"signature": "sig"},
            }
        )

        self.assertEqual(environment["__xian_execution_mode__"], "xian_vm_v2")
        self.assertEqual(environment["block_hash"], "abc123")

    def test_execute_tx_rejects_submission_without_artifacts_for_xian_vm(self):
        driver = _driver()
        driver.discarding_changes = contextlib.nullcontext
        processor = _processor(driver)

        with (
            mock.patch("xian.execution_engine.prepare_vm_contract") as prepare,
            mock.patch("xian.execution_engine._load_vm_runtime_bindings") as bindings,
        ):
            output = processor.execute_tx(
                transaction=_transaction(
                    sender="sys",
                    contract="submission",
                    function="submit_contract",
                    kwargs={
                        "name": "con_probe",
                    },
                ),
                chi_cost=20,
                environment={},
                metering=False,
            )

        self.assertEqual(output["status_code"], 1)
        self.assertIn("requires source code", str(output["result"]))
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "submission",
        )
        bindings.return_value.execute_contract.assert_not_called()

    def test_execute_tx_runs_vm_authoritative_execution(self):
        processor = _processor(_driver())

        with (
            mock.patch(
                "xian.processor.execute_vm_transaction",
                return_value=SimpleNamespace(
                    output=SimpleNamespace(
                        status_code=0,
                        result="ok",
                        events=[],
                    ),
                    writes={"currency.balances:alice": 5},
                    chi_used=11,
                    contract_costs={"currency": 100},
                ),
            ) as native_execute,
        ):
            output = processor.execute_tx(
                transaction=_transaction(),
                chi_cost=20,
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 7,
                    "block_hash": "abc123",
                    "chain_id": "xian-local",
                },
                metering=True,
                charge_fees=False,
            )

        self.assertEqual(output["status_code"], 0)
        self.assertEqual(output["chi_used"], 11)
        self.assertEqual(output["writes"], {"currency.balances:alice": 5})
        self.assertEqual(output["contract_costs"], {"currency": 100})
        native_execute.assert_called_once()
        self.assertFalse(native_execute.call_args.kwargs["apply_metering_writes"])


if __name__ == "__main__":
    unittest.main()
