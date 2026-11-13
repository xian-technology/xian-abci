import unittest

from xian.execution_policy import (
    validate_vm_execution_config,
    validate_vm_execution_mode,
)


class VmExecutionConfigTests(unittest.TestCase):
    def test_validate_defaults_to_xian_vm(self):
        self.assertIsNone(validate_vm_execution_mode())

    def test_validate_rejects_non_vm_modes(self):
        with self.assertRaisesRegex(ValueError, "xian_vm_v2"):
            validate_vm_execution_mode(mode="native_instruction_v1")

    def test_validate_vm_execution_config_rejects_execution_section(self):
        with self.assertRaisesRegex(ValueError, "xian.execution"):
            validate_vm_execution_config({"execution": {"engine": {"mode": "xian_vm_v2"}}})

    def test_validate_vm_execution_config_rejects_tracer_mode(self):
        with self.assertRaisesRegex(ValueError, "tracer_mode"):
            validate_vm_execution_config({"tracer_mode": "python_line_v1"})

    def test_validate_vm_execution_config_rejects_nested_execution_fields(self):
        with self.assertRaisesRegex(ValueError, "xian.execution"):
            validate_vm_execution_config(
                {
                    "execution": {
                        "engine": {
                            "mode": "xian_vm_v2",
                            "shadow_tracer_mode": "python_line_v1",
                        }
                    }
                },
            )

    def test_validate_vm_execution_config_rejects_removed_parallel_execution_keys(self):
        with self.assertRaises(ValueError) as raised:
            validate_vm_execution_config(
                {
                    "bds_enabled": False,
                    "parallel_execution_workers": 4,
                    "parallel_execution_enabled": False,
                }
            )
        message = str(raised.exception)
        self.assertIn("parallel_execution_enabled, parallel_execution_workers", message)
        self.assertIn("remove", message)

    def test_validate_vm_execution_config_accepts_the_generated_template(self):
        from xian.node_setup import DEFAULT_XIAN_CONFIG_TOML
        from xian import toml_utils

        self.assertIsNone(validate_vm_execution_config(toml_utils.loads(DEFAULT_XIAN_CONFIG_TOML)))
