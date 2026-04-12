import unittest

from xian.execution_policy import (
    ExecutionPolicy,
    load_execution_policy,
    resolve_execution_policy,
)


class ExecutionPolicyTests(unittest.TestCase):
    def test_resolve_current_tracer_mode(self):
        policy = resolve_execution_policy(mode="native_instruction_v1")

        self.assertEqual(
            policy,
            ExecutionPolicy(mode="native_instruction_v1"),
        )
        self.assertTrue(policy.is_current_tracer_mode)

    def test_resolve_future_mode_requires_metadata(self):
        with self.assertRaisesRegex(ValueError, "bytecode_version"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                allow_future=True,
                gas_schedule="xvm-gas-1",
            )

        with self.assertRaisesRegex(ValueError, "gas_schedule"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                allow_future=True,
                bytecode_version="xvm-1",
            )

        with self.assertRaisesRegex(ValueError, "shadow_tracer_mode"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                allow_future=True,
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
            )

    def test_resolve_future_mode_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "not implemented"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
            )

    def test_load_execution_policy_prefers_nested_execution_engine(self):
        policy = load_execution_policy(
            {
                "tracer_mode": "native_instruction_v1",
                "execution": {
                    "engine": {
                        "mode": "native_instruction_v1",
                    }
                },
            }
        )

        self.assertEqual(policy.mode, "native_instruction_v1")

    def test_load_execution_policy_rejects_mismatched_legacy_mode(self):
        with self.assertRaisesRegex(ValueError, "must match"):
            load_execution_policy(
                {
                    "tracer_mode": "python_line_v1",
                    "execution": {
                        "engine": {
                            "mode": "native_instruction_v1",
                        }
                    },
                }
            )

    def test_resolve_future_mode_accepts_shadow_tracer_mode(self):
        policy = resolve_execution_policy(
            mode="xian_vm_v1",
            allow_future=True,
            bytecode_version="xvm-1",
            gas_schedule="xvm-gas-1",
            shadow_tracer_mode="native_instruction_v1",
        )

        self.assertEqual(policy.mode, "xian_vm_v1")
        self.assertEqual(policy.authority, "python")
        self.assertEqual(policy.shadow_tracer_mode, "native_instruction_v1")

    def test_resolve_future_mode_accepts_native_authority(self):
        policy = resolve_execution_policy(
            mode="xian_vm_v1",
            allow_future=True,
            bytecode_version="xvm-1",
            gas_schedule="xvm-gas-1",
            authority="native",
        )

        self.assertEqual(policy.mode, "xian_vm_v1")
        self.assertEqual(policy.authority, "native")
        self.assertEqual(policy.shadow_tracer_mode, "")

    def test_resolve_future_mode_rejects_invalid_shadow_tracer_mode(self):
        with self.assertRaisesRegex(ValueError, "shadow_tracer_mode"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                allow_future=True,
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                shadow_tracer_mode="not-a-tracer",
            )

    def test_resolve_future_mode_rejects_invalid_authority(self):
        with self.assertRaisesRegex(ValueError, "authority"):
            resolve_execution_policy(
                mode="xian_vm_v1",
                allow_future=True,
                bytecode_version="xvm-1",
                gas_schedule="xvm-gas-1",
                authority="fallback",
                shadow_tracer_mode="python_line_v1",
            )
