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


class ConfigureNodeFeePolicyTests(unittest.TestCase):
    def test_fee_policy_flags_are_genesis_inputs(self):
        from unittest import mock

        from xian.cli import configure_node

        with self.assertRaisesRegex(ValueError, "require --genesis-bundle"):
            configure_node.main(["--moniker", "v", "--tx-fee-mode", "free_metered"])

        with (
            mock.patch.object(configure_node, "build_bundle_network_genesis") as build,
            mock.patch.object(configure_node, "configure_existing_home", return_value={}),
        ):
            build.return_value = {}
            configure_node.main(
                [
                    "--moniker",
                    "v",
                    "--genesis-bundle",
                    "devnet",
                    "--chain-id",
                    "xian-devnet-1",
                    "--genesis-time",
                    "2026-03-30T00:00:00Z",
                    "--tx-fee-mode",
                    "free_metered",
                    "--free-tx-max-chi",
                    "500",
                ]
            )
        self.assertEqual(
            build.call_args.kwargs["fee_policy"],
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 500},
        )


    def test_bundle_genesis_requires_a_pinned_genesis_time(self):
        from xian.cli import configure_node

        with self.assertRaisesRegex(ValueError, "--genesis-time"):
            configure_node.main(
                ["--moniker", "v", "--genesis-bundle", "devnet", "--chain-id", "xian-devnet-1"]
            )
