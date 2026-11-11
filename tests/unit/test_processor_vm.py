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
    def test_access_includes_fee_and_reward_configuration_reads(self):
        driver = _driver()
        driver.set = lambda **_kwargs: None
        processor = _processor(driver)
        processor.chi_cost_key = "chi_cost.S:value"
        output = {
            "status_code": 0,
            "result": None,
            "writes": {},
            "events": [],
            "chi_used": 1,
            "reads": {"con_probe.value": 1},
            "prefix_reads": frozenset({"con_probe.values:"}),
        }

        def reward_outputs(**_kwargs):
            driver.transaction_reads.update(
                {"rewards.S:value": [1, 0, 0, 0], "foundation.owner": "foundation"}
            )
            driver.transaction_read_prefixes.add("validators.delegations:")
            return None, {}, []

        rewards_handler = SimpleNamespace(build_tx_reward_outputs=reward_outputs)
        with mock.patch("xian.processor.tx_hash_from_tx", return_value="tx-hash"):
            result = processor.process_tx_output(
                output,
                _transaction(contract="con_probe", function="run", kwargs={}),
                chi_cost=20,
                rewards_handler=rewards_handler,
                track_access=True,
            )

        self.assertEqual(
            result["reads"],
            frozenset(
                {
                    "con_probe.value",
                    "chi_cost.S:value",
                    "rewards.S:value",
                    "foundation.owner",
                }
            ),
        )
        self.assertEqual(
            result["prefix_reads"],
            frozenset({"con_probe.values:", "validators.delegations:"}),
        )

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
            reads={},
            prefix_reads=frozenset(),
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

        access = processor.estimate_access(_transaction(function="custom", kwargs={}))

        self.assertIsNone(access)

    def test_estimate_access_for_core_value_reader(self):
        processor = _processor(_driver())

        access = processor.estimate_access(
            _transaction(contract="chi_cost", function="current_value", kwargs={})
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
        driver.snapshot_state = lambda: None
        driver.restore_state = lambda _state: None
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
