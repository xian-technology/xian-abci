import unittest
from types import SimpleNamespace
from unittest import mock

from xian_runtime_types.time import Datetime

from xian.processor import TxProcessor


def _driver():
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
    driver.clear_transaction_reads = lambda: None
    driver.clear_transaction_writes = lambda: None
    driver.make_key = lambda contract, variable, args: (
        f"{contract}.{variable}:" + ":".join(str(arg) for arg in args)
    )
    return driver


def _processor(driver):
    processor = object.__new__(TxProcessor)
    processor.client = SimpleNamespace(raw_driver=driver)
    processor.execution_runtime = SimpleNamespace(mode="xian_vm_v1")
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
    def test_estimate_access_for_token_transfer(self):
        processor = _processor(_driver())

        access = processor.estimate_access(_transaction())

        expected_keys = frozenset(
            {
                "currency.balances:alice",
                "currency.balances:bob",
            }
        )
        self.assertIsNotNone(access)
        self.assertEqual(access.sender, "alice")
        self.assertEqual(access.reads, expected_keys)
        self.assertEqual(access.writes, expected_keys)
        self.assertEqual(access.prefix_reads, frozenset())

    def test_estimate_access_returns_none_for_unknown_function(self):
        processor = _processor(_driver())

        access = processor.estimate_access(
            _transaction(function="custom", kwargs={})
        )

        self.assertIsNone(access)

    def test_estimate_access_for_core_value_reader(self):
        processor = _processor(_driver())

        access = processor.estimate_access(
            _transaction(
                contract="chi_cost", function="current_value", kwargs={}
            )
        )

        self.assertIsNotNone(access)
        self.assertEqual(access.reads, frozenset({"chi_cost.S:value"}))
        self.assertEqual(access.writes, frozenset())

    def test_estimate_access_for_core_value_writer(self):
        processor = _processor(_driver())

        access = processor.estimate_access(
            _transaction(
                contract="rewards",
                function="set_value",
                kwargs={"new_value": [0.25, 0.25, 0.25, 0.25]},
            )
        )

        self.assertIsNotNone(access)
        self.assertEqual(access.reads, frozenset())
        self.assertEqual(access.writes, frozenset({"rewards.S:value"}))

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
                reads={},
                prefix_reads=frozenset(),
                contract_costs={},
            )

        with (
            mock.patch("xian.processor.prepare_vm_contract"),
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
        processor.execution_runtime = SimpleNamespace(mode="xian_vm_v1")
        processor.get_timestamp_hash_from_tx = lambda nanos, signature: (
            f"{nanos}:{signature}"
        )
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

        self.assertEqual(environment["__xian_execution_mode__"], "xian_vm_v1")
        self.assertEqual(environment["block_hash"], "abc123")

    def test_execute_tx_rejects_submission_without_artifacts_for_xian_vm(self):
        processor = _processor(_driver())

        with (
            mock.patch("xian.processor.prepare_vm_contract") as prepare,
            mock.patch(
                "xian.processor.execute_vm_transaction"
            ) as native_execute,
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
        self.assertIn("requires deployment_artifacts", str(output["result"]))
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "submission",
        )
        native_execute.assert_not_called()

    def test_execute_tx_runs_vm_authoritative_execution(self):
        processor = _processor(_driver())

        with (
            mock.patch("xian.processor.prepare_vm_contract") as prepare,
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
                    reads={"currency.balances:alice": 10},
                    prefix_reads=frozenset({"currency.balances"}),
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
            )

        self.assertEqual(output["status_code"], 0)
        self.assertEqual(output["chi_used"], 11)
        self.assertEqual(output["writes"], {"currency.balances:alice": 5})
        self.assertEqual(output["contract_costs"], {"currency": 100})
        prepare.assert_called_once_with(
            processor.execution_runtime,
            processor.client.raw_driver,
            "currency",
        )
        native_execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
