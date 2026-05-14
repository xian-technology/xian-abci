import unittest

from xian.cli.configure_node import build_parser


class ConfigureNodeCliTests(unittest.TestCase):
    def test_parser_does_not_expose_execution_policy_flags(self):
        args = build_parser().parse_args(
            [
                "--moniker",
                "validator-1",
                "--copy-genesis",
            ]
        )

        self.assertFalse(hasattr(args, "execution_bytecode_version"))
        self.assertFalse(hasattr(args, "execution_gas_schedule"))
        self.assertFalse(hasattr(args, "execution_authority"))

    def test_parser_rejects_removed_execution_policy_flags(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "--moniker",
                    "validator-1",
                    "--copy-genesis",
                    "--execution-authority",
                    "native",
                ]
            )
