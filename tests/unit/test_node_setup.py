import json
import tempfile
import unittest
from pathlib import Path

from xian.node_setup import (
    build_priv_validator_key,
    generate_validator_material,
    materialize_cometbft_home,
    render_cometbft_config,
)
from xian.toml_utils import load as load_toml


class NodeSetupTests(unittest.TestCase):
    def test_render_config_applies_xian_settings(self):
        config = render_cometbft_config(
            moniker="validator-1",
            seed_nodes=["seed1@127.0.0.1:26656", "seed2@127.0.0.1:26656"],
            service_node=True,
            enable_pruning=True,
            blocks_to_keep=5000,
            parallel_execution_enabled=True,
            parallel_execution_workers=4,
            parallel_execution_min_transactions=12,
            allow_cors=False,
        )

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
        self.assertTrue(config["xian"]["block_service_mode"])
        self.assertTrue(config["xian"]["pruning_enabled"])
        self.assertEqual(config["xian"]["blocks_to_keep"], 5000)
        self.assertEqual(config["xian"]["tracer_mode"], "python_line_v1")
        self.assertTrue(config["xian"]["parallel_execution_enabled"])
        self.assertEqual(config["xian"]["parallel_execution_workers"], 4)
        self.assertEqual(
            config["xian"]["parallel_execution_min_transactions"], 12
        )
        self.assertEqual(config["xian"]["bds"]["database"], "xian")
        self.assertEqual(config["xian"]["bds"]["application_name"], "xian-bds")

    def test_render_config_applies_bds_settings(self):
        config = render_cometbft_config(
            moniker="validator-1",
            bds_host="postgres",
            bds_port=5544,
            bds_database="xian_index",
            bds_user="indexer",
            bds_password="secret",
            bds_pool_min_size=2,
            bds_pool_max_size=6,
            bds_statement_timeout_ms=5000,
            bds_application_name="xian-bds-test",
        )

        self.assertEqual(config["xian"]["bds"]["host"], "postgres")
        self.assertEqual(config["xian"]["bds"]["port"], 5544)
        self.assertEqual(config["xian"]["bds"]["database"], "xian_index")
        self.assertEqual(config["xian"]["bds"]["user"], "indexer")
        self.assertEqual(config["xian"]["bds"]["password"], "secret")
        self.assertEqual(config["xian"]["bds"]["pool_min_size"], 2)
        self.assertEqual(config["xian"]["bds"]["pool_max_size"], 6)
        self.assertEqual(config["xian"]["bds"]["statement_timeout_ms"], 5000)
        self.assertEqual(
            config["xian"]["bds"]["application_name"], "xian-bds-test"
        )

    def test_render_config_supports_native_tracer_mode(self):
        config = render_cometbft_config(
            moniker="validator-1",
            tracer_mode="native_instruction_v1",
        )

        self.assertEqual(config["xian"]["tracer_mode"], "native_instruction_v1")

    def test_render_config_supports_periodic_block_policy(self):
        config = render_cometbft_config(
            moniker="validator-1",
            block_policy_mode="periodic",
            block_policy_interval="10s",
        )

        self.assertTrue(config["consensus"]["create_empty_blocks"])
        self.assertEqual(
            config["consensus"]["create_empty_blocks_interval"], "10s"
        )

    def test_render_config_supports_idle_interval_block_policy(self):
        config = render_cometbft_config(
            moniker="validator-1",
            block_policy_mode="idle_interval",
            block_policy_interval="10s",
        )

        self.assertFalse(config["consensus"]["create_empty_blocks"])
        self.assertEqual(
            config["consensus"]["create_empty_blocks_interval"], "10s"
        )

    def test_render_config_rejects_zero_interval_for_periodic_modes(self):
        with self.assertRaisesRegex(ValueError, "non-zero"):
            render_cometbft_config(
                moniker="validator-1",
                block_policy_mode="periodic",
                block_policy_interval="0s",
            )

    def test_materialize_home_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / ".cometbft"
            config = render_cometbft_config(moniker="validator-1")
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
                genesis=genesis,
                priv_validator_key=priv_validator_key,
            )

            config_path = Path(result["config_path"])
            genesis_path = Path(result["genesis_path"])
            priv_validator_key_path = Path(result["priv_validator_key_path"])
            node_key_path = Path(result["node_key_path"])
            state_path = Path(result["priv_validator_state_path"])
            storage_path = Path(result["storage_path"])

            self.assertTrue(config_path.exists())
            self.assertTrue(genesis_path.exists())
            self.assertTrue(priv_validator_key_path.exists())
            self.assertTrue(node_key_path.exists())
            self.assertTrue(state_path.exists())
            self.assertTrue(storage_path.exists())

            rendered_config = load_toml(config_path)
            self.assertEqual(rendered_config["moniker"], "validator-1")

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
            initial_config = render_cometbft_config(moniker="validator-1")
            updated_config = render_cometbft_config(moniker="validator-2")
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
