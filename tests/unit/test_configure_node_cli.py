import unittest

from xian.cli.configure_node import build_parser


class ConfigureNodeCliTests(unittest.TestCase):
    def test_parser_does_not_expose_execution_policy_flags(self):
        args = build_parser().parse_args(
            [
                "--moniker",
                "validator-1",
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
                    "--execution-authority",
                    "native",
                ]
            )

    def test_parser_accepts_current_chain_setup_flags(self):
        args = build_parser().parse_args(
            [
                "--moniker",
                "validator-1",
                "--bds-enabled",
                "--seed",
                "seed-id@127.0.0.1:26656",
                "--persistent-peer",
                "peer-id@127.0.0.1:26656",
                "--genesis-bundle",
                "devnet",
                "--chain-id",
                "xian-devnet-1",
                "--bds-acquire-timeout-ms",
                "15000",
                "--bds-queue-max-size",
                "321",
                "--no-bds-catchup-enabled",
                "--bds-catchup-poll-seconds",
                "2.5",
                "--bds-rpc-url",
                "http://rpc.internal:26657",
            ]
        )

        self.assertTrue(args.bds_enabled)
        self.assertEqual(args.seed, ["seed-id@127.0.0.1:26656"])
        self.assertEqual(args.persistent_peer, ["peer-id@127.0.0.1:26656"])
        self.assertEqual(args.genesis_bundle, "devnet")
        self.assertEqual(args.chain_id, "xian-devnet-1")
        self.assertEqual(args.bds_acquire_timeout_ms, 15000)
        self.assertEqual(args.bds_queue_max_size, 321)
        self.assertFalse(args.bds_catchup_enabled)
        self.assertEqual(args.bds_catchup_poll_seconds, 2.5)
        self.assertEqual(args.bds_rpc_url, "http://rpc.internal:26657")
