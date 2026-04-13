import unittest

from xian.cli.configure_node import build_parser


class ConfigureNodeCliTests(unittest.TestCase):
    def test_parser_accepts_execution_policy_flags(self):
        args = build_parser().parse_args(
            [
                "--moniker",
                "validator-1",
                "--copy-genesis",
                "--execution-mode",
                "xian_vm_v1",
                "--execution-bytecode-version",
                "xvm-1",
                "--execution-gas-schedule",
                "xvm-gas-1",
                "--execution-authority",
                "native",
            ]
        )

        self.assertEqual(args.execution_mode, "xian_vm_v1")
        self.assertEqual(args.execution_bytecode_version, "xvm-1")
        self.assertEqual(args.execution_gas_schedule, "xvm-gas-1")
        self.assertEqual(args.execution_authority, "native")
        self.assertEqual(args.execution_shadow_tracer_mode, "")

    def test_parser_defaults_execution_mode_to_none(self):
        args = build_parser().parse_args(
            [
                "--moniker",
                "validator-1",
                "--copy-genesis",
            ]
        )

        self.assertIsNone(args.execution_mode)
