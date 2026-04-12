import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from contracting.client import ContractingClient
from contracting.compilation.artifacts import build_contract_artifacts
from contracting.compilation.compiler import ContractingCompiler
from contracting.storage.driver import Driver
from xian.execution_engine import (
    _load_vm_runtime_bindings,
    build_execution_runtime,
    compare_execution_results,
    clear_prepared_contract_cache,
    execute_native_contract,
    prepare_contract_for_execution,
    restore_driver_state,
    snapshot_driver_state,
)
from xian.execution_policy import ExecutionPolicy


class ExecutionEngineRuntimeTests(unittest.TestCase):
    def setUp(self):
        _load_vm_runtime_bindings.cache_clear()
        clear_prepared_contract_cache()

    def test_build_runtime_for_current_tracer_mode(self):
        runtime = build_execution_runtime(
            ExecutionPolicy(mode="native_instruction_v1")
        )

        self.assertEqual(runtime.mode, "native_instruction_v1")
        self.assertEqual(runtime.tracer_mode, "native_instruction_v1")
        self.assertTrue(runtime.supports_transaction_execution)
        self.assertIsNone(runtime.native_runtime_info)

    def test_build_runtime_for_vm_requires_native_package(self):
        with mock.patch.dict(sys.modules, {"xian_vm_core": None}):
            with self.assertRaisesRegex(
                ValueError, "xian-tech-vm-core"
            ):
                build_execution_runtime(
                    ExecutionPolicy(
                        mode="xian_vm_v1",
                        bytecode_version="xvm-1",
                        gas_schedule="xvm-gas-1",
                        authority="python",
                        shadow_tracer_mode="python_line_v1",
                    )
                )

    def test_build_runtime_for_vm_requires_supported_policy(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {"vm_profile": "xian_vm_v1"},
            supports_execution_policy=lambda *_args: False,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            with self.assertRaisesRegex(ValueError, "does not support"):
                build_execution_runtime(
                    ExecutionPolicy(
                        mode="xian_vm_v1",
                        bytecode_version="xvm-9",
                        gas_schedule="xvm-gas-9",
                        authority="python",
                        shadow_tracer_mode="python_line_v1",
                    )
                )

    def test_build_runtime_for_vm_python_authority_requires_shadow_tracer_mode(
        self,
    ):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "ir_version": "xian_ir_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
                "supported_bytecode_versions": ["xvm-1"],
                "supported_gas_schedules": ["xvm-gas-1"],
            },
            supports_execution_policy=lambda bytecode_version, gas_schedule: (
                bytecode_version == "xvm-1"
                and gas_schedule == "xvm-gas-1"
            ),
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            with self.assertRaisesRegex(ValueError, "shadow_tracer_mode"):
                build_execution_runtime(
                    ExecutionPolicy(
                        mode="xian_vm_v1",
                        bytecode_version="xvm-1",
                        gas_schedule="xvm-gas-1",
                        authority="python",
                    )
                )

    def test_build_runtime_for_vm_shadow_mode_enables_transaction_path(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="python",
                    shadow_tracer_mode="python_line_v1",
                )
            )

        self.assertTrue(runtime.supports_transaction_execution)
        self.assertTrue(runtime.shadow_execution)
        self.assertFalse(runtime.native_authoritative)
        self.assertEqual(runtime.authority, "python")
        self.assertEqual(runtime.tracer_mode, "python_line_v1")
        self.assertEqual(runtime.shadow_tracer_mode, "python_line_v1")
        self.assertEqual(runtime.unavailable_reason, "")

    def test_build_runtime_for_vm_native_authority_enables_native_execution(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            )

        self.assertTrue(runtime.supports_transaction_execution)
        self.assertFalse(runtime.shadow_execution)
        self.assertTrue(runtime.native_authoritative)
        self.assertEqual(runtime.authority, "native")
        self.assertIsNone(runtime.tracer_mode)

    def test_prepare_contract_for_execution_recurses_static_imports(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
            validate_module_ir=mock.Mock(),
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            built_runtime = build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="python",
                    shadow_tracer_mode="python_line_v1",
                )
            )
            driver = mock.Mock()
            driver.get_contract_ir.side_effect = (
                lambda name, vm_profile="xian_vm_v1": {
                    "con_parent": (
                        ContractingCompiler(
                            module_name="con_parent"
                        ).lower_to_ir_json(
                            "import con_child\n\n@export\ndef ping():\n    return con_child.ping()\n",
                            vm_profile="xian_vm_v1",
                            indent=None,
                            sort_keys=True,
                        )
                    ),
                    "con_child": (
                        ContractingCompiler(
                            module_name="con_child"
                        ).lower_to_ir_json(
                            "@export\ndef ping():\n    return 'pong'\n",
                            vm_profile="xian_vm_v1",
                            indent=None,
                            sort_keys=True,
                        )
                    ),
                }.get(name)
            )
            driver.get_contract_source.return_value = None
            prepared = prepare_contract_for_execution(
                built_runtime,
                driver,
                "con_parent",
            )

        self.assertEqual(prepared.contract_name, "con_parent")
        self.assertEqual(prepared.imported_contracts, ("con_child",))
        self.assertEqual(fake_bindings.validate_module_ir.call_count, 2)
        driver.get_contract_source.assert_not_called()

    def test_prepare_contract_for_execution_accepts_persisted_vm_ir(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
            validate_module_ir=mock.Mock(),
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            )

            driver = mock.Mock()
            parent_ir = ContractingCompiler(
                module_name="con_parent"
            ).lower_to_ir_json(
                "import con_child\n\n@export\ndef ping():\n    return con_child.ping()\n",
                vm_profile="xian_vm_v1",
                indent=None,
                sort_keys=True,
            )
            child_ir = ContractingCompiler(
                module_name="con_child"
            ).lower_to_ir_json(
                "@export\ndef ping():\n    return 'pong'\n",
                vm_profile="xian_vm_v1",
                indent=None,
                sort_keys=True,
            )
            driver.get_contract_ir.side_effect = (
                lambda name, vm_profile="xian_vm_v1": {
                    "con_parent": parent_ir,
                    "con_child": child_ir,
                }.get(name)
            )
            driver.get_contract_source.return_value = None

            prepared = prepare_contract_for_execution(
                runtime,
                driver,
                "con_parent",
            )

        self.assertEqual(prepared.contract_name, "con_parent")
        self.assertEqual(prepared.imported_contracts, ("con_child",))
        self.assertEqual(fake_bindings.validate_module_ir.call_count, 2)
        driver.get_contract_source.assert_not_called()

    def test_execute_native_submission_deploy_stages_contract_artifacts(self):
        runtime = build_execution_runtime(
            ExecutionPolicy(
                mode="xian_vm_v1",
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                authority="native",
            )
        )
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        code = (
            "v = Variable()\n\n"
            "@construct\n"
            "def seed(value: int = 1):\n"
            "    v.set(value)\n\n"
            "@export\n"
            "def ping():\n"
            "    return v.get()\n"
        )
        artifacts = build_contract_artifacts(
            module_name="con_native_submission_probe",
            source=code,
            lint=True,
            vm_profile="xian_vm_v1",
        )

        output = execute_native_contract(
            runtime,
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_native_submission_probe",
                "code": None,
                "deployment_artifacts": artifacts,
                "constructor_args": {"value": 9},
            },
            environment={},
            meter=True,
            chi_budget=10_000,
            transaction_size_bytes=256,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_native_submission_probe.__code__", output.writes)
        self.assertIn("con_native_submission_probe.__source__", output.writes)
        self.assertIn(
            "con_native_submission_probe.__xian_ir_v1__", output.writes
        )
        self.assertEqual(output.writes["con_native_submission_probe.v"], 9)
        self.assertTrue(
            any(
                event.get("event") == "ContractDeployed"
                for event in output.events
            )
        )
        self.assertGreater(output.chi_used, 0)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_execute_native_submission_change_owner_stages_metadata_write(self):
        runtime = build_execution_runtime(
            ExecutionPolicy(
                mode="xian_vm_v1",
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                authority="native",
            )
        )
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        driver.set_contract_from_source(
            "con_owned",
            "@export\ndef ping():\n    return 1\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )

        output = execute_native_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="submission",
            function_name="change_owner",
            kwargs={"contract": "con_owned", "new_owner": "bob"},
            environment={},
        )

        self.assertEqual(output.status_code, 0)
        self.assertEqual(output.writes["con_owned.__owner__"], "bob")
        self.assertTrue(
            any(
                event.get("event") == "ContractOwnerChanged"
                for event in output.events
            )
        )
        driver.flush_full()

    def test_execute_native_token_factory_deploys_child_contract(self):
        runtime = build_execution_runtime(
            ExecutionPolicy(
                mode="xian_vm_v1",
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                authority="native",
            )
        )
        driver = Driver()
        driver.flush_full()
        ContractingClient(driver=driver)
        token_factory_source = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
            / "token_factory.s.py"
        ).read_text()
        driver.set_contract_from_source(
            "token_factory",
            token_factory_source,
            owner="sys",
            developer="sys",
            deployer="sys",
            initiator="sys",
        )

        output = execute_native_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="token_factory",
            function_name="create_token",
            kwargs={
                "token_contract": "con_demo_token",
                "token_name": "Demo Token",
                "token_symbol": "DEMO",
                "initial_supply": 42,
                "initial_holder": "bob",
                "operator_address": "carol",
            },
            environment={},
            meter=True,
            chi_budget=100_000,
            transaction_size_bytes=512,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_demo_token.__code__", output.writes)
        self.assertIn("con_demo_token.__source__", output.writes)
        self.assertIn("con_demo_token.__xian_ir_v1__", output.writes)
        self.assertEqual(output.writes["con_demo_token.balances:bob"], 42)
        self.assertEqual(
            output.writes["con_demo_token.metadata:token_symbol"],
            "DEMO",
        )
        self.assertEqual(output.writes["con_demo_token.operator"], "carol")
        self.assertTrue(
            any(
                event.get("event") == "ContractDeployed"
                for event in output.events
            )
        )
        self.assertTrue(
            any(
                event.get("event") == "TokenCreated"
                for event in output.events
            )
        )
        self.assertIn("token_factory", output.contract_costs)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_prepare_contract_for_execution_requires_persisted_ir(
        self,
    ):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda: {
                "vm_profile": "xian_vm_v1",
                "host_catalog_version": "xian_vm_v1_host_v1",
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="python",
                    shadow_tracer_mode="python_line_v1",
                )
            )

        driver = mock.Mock()
        driver.get_contract_ir.return_value = None
        driver.get_contract_source.return_value = (
            "@export\ndef ping():\n    return 'source only'\n"
        )

        with self.assertRaisesRegex(
            ValueError,
            "requires persisted __xian_ir_v1__",
        ):
            prepare_contract_for_execution(runtime, driver, "con_missing")

        driver.get_contract_source.assert_not_called()

    def test_snapshot_driver_state_round_trips(self):
        driver = types.SimpleNamespace(
            pending_writes={"currency.balances:alice": 5},
            pending_reads={"currency.balances:alice": 1},
            pending_deltas={"currency.balances:alice": 4},
            transaction_reads={"currency.balances:alice": 1},
            transaction_read_prefixes={"currency."},
            transaction_writes={"currency.balances:alice": 5},
            log_events=[{"event": "Transfer"}],
        )

        snapshot = snapshot_driver_state(driver)
        driver.pending_writes = {}
        driver.pending_reads = {}
        driver.pending_deltas = {}
        driver.transaction_reads = {}
        driver.transaction_read_prefixes = set()
        driver.transaction_writes = {}
        driver.log_events = []

        restore_driver_state(driver, snapshot)

        self.assertEqual(driver.pending_writes, {"currency.balances:alice": 5})
        self.assertEqual(driver.transaction_read_prefixes, {"currency."})
        self.assertEqual(driver.log_events, [{"event": "Transfer"}])

    def test_execute_native_contract_converts_runtime_errors(self):
        runtime = types.SimpleNamespace(mode="xian_vm_v1")
        driver = mock.Mock()
        driver.get_owner.return_value = "alice"
        fake_bindings = types.SimpleNamespace(
            execute_contract=mock.Mock(side_effect=RuntimeError("boom"))
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            output = execute_native_contract(
                runtime,
                driver,
                sender="alice",
                contract_name="currency",
                function_name="transfer",
                kwargs={"amount": 5},
                environment={
                    "now": "ts",
                    "block_num": 7,
                    "block_hash": "abc",
                    "chain_id": "xian-local",
                },
            )

        self.assertEqual(output.status_code, 1)
        self.assertEqual(str(output.result), "boom")
        self.assertEqual(output.writes, {})
        self.assertEqual(output.events, [])

    def test_execute_native_contract_rejects_source_only_contracts(self):
        runtime = build_execution_runtime(
            ExecutionPolicy(
                mode="xian_vm_v1",
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                authority="native",
            )
        )
        driver = Driver()
        driver.flush_full()
        driver.set_contract_from_source(
            "con_source_only",
            "@export\ndef ping():\n    return 'pong'\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )
        driver.delete(driver.make_key("con_source_only", "__xian_ir_v1__"))

        output = execute_native_contract(
            runtime,
            driver,
            sender="alice",
            contract_name="con_source_only",
            function_name="ping",
            kwargs={},
            environment={},
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("requires persisted __xian_ir_v1__", str(output.result))

    def test_compare_execution_results_reports_mismatched_fields(self):
        native_output = types.SimpleNamespace(
            status_code=0,
            result={"amount": 7},
            writes={"currency.balances:alice": 7},
            events=[],
        )

        mismatches = compare_execution_results(
            {
                "status_code": 0,
                "result": {"amount": 5},
                "writes": {"currency.balances:alice": 5},
                "events": [],
            },
            native_output,
        )

        self.assertEqual(sorted(mismatches), ["result", "writes"])

    def test_compare_execution_results_can_ignore_metering_write_keys(self):
        native_output = types.SimpleNamespace(
            status_code=0,
            result={"amount": 5},
            writes={"demo.balances:alice": 5},
            events=[],
        )

        mismatches = compare_execution_results(
            {
                "status_code": 0,
                "result": {"amount": 5},
                "writes": {
                    "demo.balances:alice": 5,
                    "currency.balances:alice": "999.35",
                },
                "events": [],
            },
            native_output,
            ignore_write_keys={"currency.balances:alice"},
        )

        self.assertEqual(mismatches, {})
