import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from xian.cli.configure_node import build_parser
from xian.node_setup import BdsOptions


class ConfigureNodeCliTests(unittest.TestCase):
    def test_parser_uses_node_setup_defaults_for_every_bds_option(self):
        defaults = replace(
            BdsOptions(),
            dsn="postgresql://indexer@custom-db/custom-index",
            host="custom-db",
            port=5544,
            database="custom-index",
            user="indexer",
            password_file="/run/secrets/custom",
            pool_min_size=2,
            pool_max_size=20,
            statement_timeout_ms=1000,
            acquire_timeout_ms=2000,
            application_name="custom-indexer",
            queue_max_size=64,
            catchup_enabled=False,
            catchup_poll_seconds=2.5,
            rpc_url="http://127.0.0.1:27657",
            spool_dir="/tmp/custom-spool",
            spool_warn_entries=512,
            spool_warn_bytes=1024,
            disk_free_warn_bytes=4096,
        )
        with patch("xian.cli.configure_node.BDS_DEFAULTS", defaults):
            args = build_parser().parse_args(["--moniker", "validator-1"])

        self.assertEqual(
            {name: getattr(args, f"bds_{name}") for name in asdict(defaults)},
            asdict(defaults),
        )

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

    def test_parser_rejects_removed_parallel_execution_flags(self):
        for flag in ("--parallel-execution-enabled", "--parallel-execution-workers=4"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                build_parser().parse_args(["--moniker", "validator-1", flag])

    def test_parser_rejects_the_removed_bds_password_flag(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--moniker", "validator-1", "--bds-password", "secret"])

    def test_bds_defaults_target_the_containerized_postgres_service(self):
        args = build_parser().parse_args(["--moniker", "validator-1"])

        self.assertEqual(args.bds_host, "postgres")
        self.assertEqual(args.bds_user, "xian")
        self.assertEqual(args.bds_database, "xian")
        self.assertEqual(args.bds_password_file, "/run/secrets/xian_bds_password")
        self.assertEqual(args.bds_rpc_url, "http://127.0.0.1:26657")

    def test_parser_accepts_a_bds_password_file(self):
        args = build_parser().parse_args(
            ["--moniker", "validator-1", "--bds-password-file", "/run/secrets/custom"]
        )

        self.assertEqual(args.bds_password_file, "/run/secrets/custom")

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


class ConfigureNodeExternalAddressTests(unittest.TestCase):
    def test_external_address_reaches_the_rendered_config(self):
        from unittest import mock

        from xian.cli import configure_node

        with mock.patch.object(configure_node, "configure_existing_home", return_value={}) as cfg:
            configure_node.main(["--moniker", "v", "--p2p-external-address", "203.0.113.10:26656"])
            configure_node.main(["--moniker", "v"])

        first, second = (call.args[0].node_config for call in cfg.call_args_list)
        self.assertEqual(first.p2p_external_address, "203.0.113.10:26656")
        self.assertEqual(second.p2p_external_address, "")

    def test_genesis_sha256_reaches_the_home_configuration(self):
        from unittest import mock

        from xian.cli import configure_node

        with mock.patch.object(configure_node, "configure_existing_home", return_value={}) as cfg:
            configure_node.main(["--moniker", "v", "--genesis-sha256", "AB" * 32])
            configure_node.main(["--moniker", "v"])

        first, second = (call.args[0] for call in cfg.call_args_list)
        self.assertEqual(first.expected_genesis_sha256, "ab" * 32)
        self.assertIsNone(second.expected_genesis_sha256)
        for invalid in ("abc", "zz" * 32):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                build_parser().parse_args(["--moniker", "v", "--genesis-sha256", invalid])


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
                    "--max-tx-chi",
                    "500",
                    "--max-block-chi",
                    "900",
                ]
            )
        self.assertEqual(
            build.call_args.kwargs["fee_policy"],
            {"tx_fee_mode": "free_metered", "max_tx_chi": 500, "max_block_chi": 900},
        )

    def test_bundle_genesis_requires_a_pinned_genesis_time(self):
        from xian.cli import configure_node

        with self.assertRaisesRegex(ValueError, "--genesis-time"):
            configure_node.main(
                ["--moniker", "v", "--genesis-bundle", "devnet", "--chain-id", "xian-devnet-1"]
            )


class ConfigureNodeGenesisFlagTests(unittest.TestCase):
    def test_conflicting_or_incomplete_genesis_flags_are_refused(self):
        from xian.cli import configure_node

        bundle = ["--genesis-bundle", "devnet", "--genesis-time", "2026-03-30T00:00:00Z"]
        cases = (
            (["--genesis-time", "2026-03-30T00:00:00Z"], "--genesis-time requires"),
            (["--genesis-source", "devnet", *bundle], "not both"),
            (bundle, "--chain-id is required"),
        )
        for flags, message in cases:
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, message):
                configure_node.main(["--moniker", "v", *flags])
