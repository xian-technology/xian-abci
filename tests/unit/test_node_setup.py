import json
import tempfile
import unittest
from pathlib import Path

from xian.execution_policy import load_execution_policy
from xian.node_setup import (
    AppLoggingOptions,
    BdsOptions,
    ExecutionOptions,
    MetricsOptions,
    NodeConfigOptions,
    ParallelExecutionOptions,
    SimulationOptions,
    StateSyncOptions,
    build_priv_validator_key,
    generate_validator_material,
    materialize_cometbft_home,
    render_cometbft_config,
    render_node_configs,
    render_xian_config,
    resolve_app_logging_settings,
    resolve_simulation_settings,
    resolve_statesync_settings,
)
from xian.toml_utils import load as load_toml


def _node_options(moniker: str = "validator-1", **overrides):
    return NodeConfigOptions(moniker=moniker, **overrides)


class NodeSetupTests(unittest.TestCase):
    def test_render_config_accepts_node_config_options(self):
        configs = render_node_configs(
            options=NodeConfigOptions(
                moniker="validator-1",
                seed_nodes=("seed-1@127.0.0.1:26656",),
                allow_cors=False,
                service_node=True,
                enable_pruning=True,
                blocks_to_keep=5000,
                transaction_trace_logging=True,
                block_policy_mode="on_demand",
                block_policy_interval="0s",
                statesync=StateSyncOptions(
                    enable=True,
                    rpc_servers=(
                        "http://rpc-1.internal:26657",
                        "http://rpc-2.internal:26657",
                    ),
                    trust_height=120,
                    trust_hash="ab" * 32,
                    trust_period="336h0m0s",
                ),
                execution=ExecutionOptions(
                    tracer_mode="python_line_v1",
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                ),
                metrics=MetricsOptions(
                    enabled=True,
                    host="0.0.0.0",
                    port=9208,
                    bds_refresh_seconds=7.5,
                ),
                app_logging=AppLoggingOptions(
                    level="warning",
                    json_logging=True,
                    rotation_hours=4,
                    retention_days=12,
                ),
                simulation=SimulationOptions(
                    enabled=False,
                    max_concurrency=4,
                    timeout_ms=4500,
                    max_chi=750000,
                ),
                parallel_execution=ParallelExecutionOptions(
                    enabled=True,
                    workers=4,
                    min_transactions=12,
                ),
                pending_nonce_reservation_ttl_seconds=90.0,
                max_pending_nonces_per_sender=64,
                bds=BdsOptions(
                    host="postgres",
                    port=5544,
                    database="xian_index",
                    user="indexer",
                    password="secret",
                    pool_min_size=2,
                    pool_max_size=6,
                    statement_timeout_ms=5000,
                    application_name="xian-bds-test",
                    spool_dir="/var/lib/xian/bds-spool",
                    spool_warn_entries=512,
                    spool_warn_bytes=1_073_741_824,
                    disk_free_warn_bytes=4_294_967_296,
                ),
            )
        )
        config = configs["cometbft"]
        xian_config = configs["xian"]

        self.assertEqual(config["moniker"], "validator-1")
        self.assertEqual(
            config["p2p"]["seeds"],
            "seed-1@127.0.0.1:26656",
        )
        self.assertFalse(config["rpc"]["cors_allowed_origins"])
        self.assertNotIn("xian", config)
        self.assertTrue(xian_config["block_service_mode"])
        self.assertTrue(xian_config["pruning_enabled"])
        self.assertEqual(xian_config["metrics_port"], 9208)
        self.assertEqual(
            xian_config["execution"]["engine"]["bytecode_version"],
            "xvm-1",
        )
        self.assertEqual(
            xian_config["bds"]["application_name"], "xian-bds-test"
        )

    def test_render_config_applies_xian_settings(self):
        configs = render_node_configs(
            options=_node_options(
                seed_nodes=(
                    "seed1@127.0.0.1:26656",
                    "seed2@127.0.0.1:26656",
                ),
                service_node=True,
                enable_pruning=True,
                blocks_to_keep=5000,
                simulation=SimulationOptions(
                    enabled=True,
                    max_concurrency=3,
                    timeout_ms=2500,
                    max_chi=500000,
                ),
                parallel_execution=ParallelExecutionOptions(
                    enabled=True,
                    workers=4,
                    min_transactions=12,
                ),
                transaction_trace_logging=True,
                app_logging=AppLoggingOptions(
                    level="debug",
                    json_logging=True,
                    rotation_hours=6,
                    retention_days=14,
                ),
                allow_cors=False,
            )
        )
        config = configs["cometbft"]
        xian_config = configs["xian"]

        self.assertEqual(config["moniker"], "validator-1")
        self.assertEqual(
            config["p2p"]["seeds"],
            "seed1@127.0.0.1:26656,seed2@127.0.0.1:26656",
        )
        self.assertEqual(config["rpc"]["cors_allowed_origins"], [])
        self.assertFalse(config["consensus"]["create_empty_blocks"])
        self.assertEqual(
            config["consensus"]["create_empty_blocks_interval"], "0s"
        )
        self.assertNotIn("xian", config)
        self.assertTrue(xian_config["block_service_mode"])
        self.assertTrue(xian_config["pruning_enabled"])
        self.assertEqual(xian_config["blocks_to_keep"], 5000)
        self.assertEqual(xian_config["tracer_mode"], "python_line_v1")
        self.assertEqual(
            xian_config["execution"]["engine"]["mode"],
            "python_line_v1",
        )
        self.assertTrue(xian_config["transaction_trace_logging"])
        self.assertEqual(xian_config["app_log_level"], "DEBUG")
        self.assertTrue(xian_config["app_log_json"])
        self.assertEqual(xian_config["app_log_rotation_hours"], 6)
        self.assertEqual(xian_config["app_log_retention_days"], 14)
        self.assertTrue(xian_config["simulation_enabled"])
        self.assertEqual(xian_config["simulation_max_concurrency"], 3)
        self.assertEqual(xian_config["simulation_timeout_ms"], 2500)
        self.assertEqual(xian_config["simulation_max_chi"], 500000)
        self.assertTrue(xian_config["parallel_execution_enabled"])
        self.assertEqual(xian_config["parallel_execution_workers"], 4)
        self.assertEqual(xian_config["parallel_execution_min_transactions"], 12)
        self.assertEqual(xian_config["bds"]["database"], "xian")
        self.assertEqual(xian_config["bds"]["application_name"], "xian-bds")
        self.assertEqual(xian_config["bds"]["spool_warn_entries"], 256)
        self.assertEqual(xian_config["bds"]["spool_warn_bytes"], 536_870_912)
        self.assertEqual(
            xian_config["bds"]["disk_free_warn_bytes"], 2_147_483_648
        )

    def test_render_config_applies_bds_settings(self):
        xian_config = render_xian_config(
            options=_node_options(
                bds=BdsOptions(
                    host="postgres",
                    port=5544,
                    database="xian_index",
                    user="indexer",
                    password="secret",
                    pool_min_size=2,
                    pool_max_size=6,
                    statement_timeout_ms=5000,
                    application_name="xian-bds-test",
                    spool_dir="/var/lib/xian/bds-spool",
                    spool_warn_entries=512,
                    spool_warn_bytes=1_073_741_824,
                    disk_free_warn_bytes=4_294_967_296,
                )
            )
        )

        self.assertEqual(xian_config["bds"]["host"], "postgres")
        self.assertEqual(xian_config["bds"]["port"], 5544)
        self.assertEqual(xian_config["bds"]["database"], "xian_index")
        self.assertEqual(xian_config["bds"]["user"], "indexer")
        self.assertEqual(xian_config["bds"]["password"], "secret")
        self.assertEqual(xian_config["bds"]["pool_min_size"], 2)
        self.assertEqual(xian_config["bds"]["pool_max_size"], 6)
        self.assertEqual(xian_config["bds"]["statement_timeout_ms"], 5000)
        self.assertEqual(
            xian_config["bds"]["application_name"], "xian-bds-test"
        )
        self.assertEqual(
            xian_config["bds"]["spool_dir"], "/var/lib/xian/bds-spool"
        )
        self.assertEqual(xian_config["bds"]["spool_warn_entries"], 512)
        self.assertEqual(xian_config["bds"]["spool_warn_bytes"], 1_073_741_824)
        self.assertEqual(
            xian_config["bds"]["disk_free_warn_bytes"], 4_294_967_296
        )

    def test_render_config_supports_native_tracer_mode(self):
        xian_config = render_xian_config(
            options=_node_options(
                execution=ExecutionOptions(
                    tracer_mode="native_instruction_v1"
                )
            )
        )

        self.assertEqual(xian_config["tracer_mode"], "native_instruction_v1")
        self.assertEqual(
            load_execution_policy(xian_config).mode,
            "native_instruction_v1",
        )

    def test_render_config_supports_future_execution_policy_shape(self):
        xian_config = render_xian_config(
            options=_node_options(
                execution=ExecutionOptions(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                )
            )
        )

        self.assertEqual(xian_config["tracer_mode"], "python_line_v1")
        self.assertEqual(
            xian_config["execution"]["engine"]["bytecode_version"],
            "xvm-1",
        )
        self.assertEqual(
            xian_config["execution"]["engine"]["gas_schedule"],
            "xvm-gas-1",
        )
        self.assertEqual(
            xian_config["execution"]["engine"]["authority"],
            "native",
        )
        self.assertNotIn(
            "shadow_tracer_mode",
            xian_config["execution"]["engine"],
        )

    def test_render_config_supports_native_vm_authority_without_shadow(self):
        xian_config = render_xian_config(
            options=_node_options(
                execution=ExecutionOptions(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            )
        )

        self.assertEqual(xian_config["tracer_mode"], "python_line_v1")
        self.assertEqual(
            xian_config["execution"]["engine"]["mode"],
            "xian_vm_v1",
        )
        self.assertEqual(
            xian_config["execution"]["engine"]["authority"],
            "native",
        )
        self.assertNotIn(
            "shadow_tracer_mode",
            xian_config["execution"]["engine"],
        )

    def test_render_config_rejects_legacy_kwargs(self):
        with self.assertRaises(TypeError):
            render_node_configs(moniker="validator-1")

    def test_render_config_supports_state_sync(self):
        config = render_cometbft_config(
            options=_node_options(
                statesync=StateSyncOptions(
                    enable=True,
                    rpc_servers=(
                        "http://rpc-1.internal:26657",
                        "http://rpc-2.internal:26657",
                    ),
                    trust_height=120,
                    trust_hash="ab" * 32,
                    trust_period="336h0m0s",
                )
            )
        )

        self.assertTrue(config["statesync"]["enable"])
        self.assertEqual(
            config["statesync"]["rpc_servers"],
            "http://rpc-1.internal:26657,http://rpc-2.internal:26657",
        )
        self.assertEqual(config["statesync"]["trust_height"], 120)
        self.assertEqual(config["statesync"]["trust_hash"], "ab" * 32)
        self.assertEqual(config["statesync"]["trust_period"], "336h0m0s")

    def test_resolve_simulation_settings_requires_positive_limits(self):
        with self.assertRaises(ValueError):
            resolve_simulation_settings(max_concurrency=0)

        with self.assertRaises(ValueError):
            resolve_simulation_settings(timeout_ms=0)

        with self.assertRaises(ValueError):
            resolve_simulation_settings(max_chi=0)

    def test_resolve_app_logging_settings_normalizes_and_validates(self):
        resolved = resolve_app_logging_settings(
            level="warning",
            json_logging=True,
            rotation_hours=2,
            retention_days=9,
        )

        self.assertEqual(resolved["level"], "WARNING")
        self.assertTrue(resolved["json"])
        self.assertEqual(resolved["rotation_hours"], 2)
        self.assertEqual(resolved["retention_days"], 9)

        with self.assertRaisesRegex(ValueError, "app_log_level"):
            resolve_app_logging_settings(level="verbose")

        with self.assertRaisesRegex(ValueError, "rotation_hours"):
            resolve_app_logging_settings(rotation_hours=0)

        with self.assertRaisesRegex(ValueError, "retention_days"):
            resolve_app_logging_settings(retention_days=0)

    def test_resolve_statesync_settings_rejects_incomplete_config(self):
        with self.assertRaisesRegex(ValueError, "at least two RPC servers"):
            resolve_statesync_settings(
                enable=True,
                rpc_servers=["http://rpc-1.internal:26657"],
                trust_height=120,
                trust_hash="ab" * 32,
            )

        with self.assertRaisesRegex(ValueError, "greater than zero"):
            resolve_statesync_settings(
                enable=True,
                rpc_servers=[
                    "http://rpc-1.internal:26657",
                    "http://rpc-2.internal:26657",
                ],
                trust_height=0,
                trust_hash="ab" * 32,
            )

    def test_render_config_supports_periodic_block_policy(self):
        config = render_cometbft_config(
            options=_node_options(
                block_policy_mode="periodic",
                block_policy_interval="10s",
            )
        )

        self.assertTrue(config["consensus"]["create_empty_blocks"])
        self.assertEqual(
            config["consensus"]["create_empty_blocks_interval"], "10s"
        )

    def test_render_config_supports_idle_interval_block_policy(self):
        config = render_cometbft_config(
            options=_node_options(
                block_policy_mode="idle_interval",
                block_policy_interval="10s",
            )
        )

        self.assertFalse(config["consensus"]["create_empty_blocks"])
        self.assertEqual(
            config["consensus"]["create_empty_blocks_interval"], "10s"
        )

    def test_render_config_rejects_zero_interval_for_periodic_modes(self):
        with self.assertRaisesRegex(ValueError, "non-zero"):
            render_cometbft_config(
                options=_node_options(
                    block_policy_mode="periodic",
                    block_policy_interval="0s",
                )
            )

    def test_materialize_home_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / ".cometbft"
            config = render_cometbft_config(options=_node_options())
            xian_config = render_xian_config(options=_node_options())
            genesis = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {},
            }
            priv_validator_key = build_priv_validator_key(
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            )

            result = materialize_cometbft_home(
                home=home,
                config=config,
                xian_config=xian_config,
                genesis=genesis,
                priv_validator_key=priv_validator_key,
            )

            config_path = Path(result["config_path"])
            xian_config_path = Path(result["xian_config_path"])
            genesis_path = Path(result["genesis_path"])
            priv_validator_key_path = Path(result["priv_validator_key_path"])
            node_key_path = Path(result["node_key_path"])
            state_path = Path(result["priv_validator_state_path"])
            storage_path = Path(result["storage_path"])

            self.assertTrue(config_path.exists())
            self.assertTrue(xian_config_path.exists())
            self.assertTrue(genesis_path.exists())
            self.assertTrue(priv_validator_key_path.exists())
            self.assertTrue(node_key_path.exists())
            self.assertTrue(state_path.exists())
            self.assertTrue(storage_path.exists())

            rendered_config = load_toml(config_path)
            rendered_xian_config = load_toml(xian_config_path)
            self.assertEqual(rendered_config["moniker"], "validator-1")
            self.assertNotIn("xian", rendered_config)
            self.assertEqual(
                rendered_xian_config["tracer_mode"], "python_line_v1"
            )

            rendered_genesis = json.loads(
                genesis_path.read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-1")

            rendered_validator = json.loads(
                priv_validator_key_path.read_text(encoding="utf-8")
            )
            self.assertIn("address", rendered_validator)
            self.assertNotIn("_private_key_hex", rendered_validator)

    def test_build_priv_validator_key_rejects_invalid_hex(self):
        with self.assertRaisesRegex(
            ValueError, "private key must be a 64-character hex string"
        ):
            build_priv_validator_key("abcd")

        with self.assertRaisesRegex(
            ValueError, "private key must be valid hex"
        ):
            build_priv_validator_key("z" * 64)

    def test_generate_validator_material_returns_expected_shape(self):
        payload = generate_validator_material(
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )

        self.assertEqual(
            payload["validator_private_key_hex"],
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(len(payload["validator_public_key_hex"]), 64)
        self.assertIn("address", payload["priv_validator_key"])
        self.assertNotIn("_private_key_hex", payload["priv_validator_key"])

    def test_materialize_home_preserves_node_key_and_state_on_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / ".cometbft"
            initial_config = render_cometbft_config(options=_node_options())
            updated_config = render_cometbft_config(
                options=_node_options("validator-2")
            )
            initial_xian_config = render_xian_config(options=_node_options())
            updated_xian_config = render_xian_config(
                options=_node_options("validator-2")
            )
            initial_genesis = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {},
            }
            updated_genesis = {
                "chain_id": "xian-local-2",
                "validators": [],
                "abci_genesis": {},
            }
            initial_validator_key = build_priv_validator_key(
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            )
            updated_validator_key = build_priv_validator_key(
                "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
            )

            first_result = materialize_cometbft_home(
                home=home,
                config=initial_config,
                xian_config=initial_xian_config,
                genesis=initial_genesis,
                priv_validator_key=initial_validator_key,
            )
            node_key_path = Path(first_result["node_key_path"])
            state_path = Path(first_result["priv_validator_state_path"])
            original_node_key = node_key_path.read_text(encoding="utf-8")
            original_state = state_path.read_text(encoding="utf-8")

            second_result = materialize_cometbft_home(
                home=home,
                config=updated_config,
                xian_config=updated_xian_config,
                genesis=updated_genesis,
                priv_validator_key=updated_validator_key,
                overwrite=True,
            )

            self.assertEqual(
                node_key_path.read_text(encoding="utf-8"), original_node_key
            )
            self.assertEqual(
                state_path.read_text(encoding="utf-8"), original_state
            )

            rendered_config = load_toml(second_result["config_path"])
            rendered_genesis = json.loads(
                Path(second_result["genesis_path"]).read_text(encoding="utf-8")
            )
            rendered_validator = json.loads(
                Path(second_result["priv_validator_key_path"]).read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(rendered_config["moniker"], "validator-2")
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-2")
            self.assertEqual(
                rendered_validator["address"],
                updated_validator_key["address"],
            )


if __name__ == "__main__":
    unittest.main()
