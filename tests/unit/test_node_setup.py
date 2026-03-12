import json
import tempfile
import unittest
from pathlib import Path

import toml

from xian.node_setup import (
    build_priv_validator_key,
    materialize_cometbft_home,
    render_cometbft_config,
)


class NodeSetupTests(unittest.TestCase):
    def test_render_config_applies_xian_settings(self):
        config = render_cometbft_config(
            moniker="validator-1",
            seed_nodes=["seed1@127.0.0.1:26656", "seed2@127.0.0.1:26656"],
            service_node=True,
            enable_pruning=True,
            blocks_to_keep=5000,
            allow_cors=False,
        )

        self.assertEqual(config["moniker"], "validator-1")
        self.assertEqual(
            config["p2p"]["seeds"],
            "seed1@127.0.0.1:26656,seed2@127.0.0.1:26656",
        )
        self.assertEqual(config["rpc"]["cors_allowed_origins"], [])
        self.assertTrue(config["xian"]["block_service_mode"])
        self.assertTrue(config["xian"]["pruning_enabled"])
        self.assertEqual(config["xian"]["blocks_to_keep"], 5000)

    def test_materialize_home_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / ".cometbft"
            config = render_cometbft_config(moniker="validator-1")
            genesis = {"chain_id": "xian-local-1", "validators": [], "abci_genesis": {}}
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

            rendered_config = toml.load(config_path)
            self.assertEqual(rendered_config["moniker"], "validator-1")

            rendered_genesis = json.loads(genesis_path.read_text(encoding="utf-8"))
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-1")

            rendered_validator = json.loads(
                priv_validator_key_path.read_text(encoding="utf-8")
            )
            self.assertIn("address", rendered_validator)
            self.assertNotIn("_private_key_hex", rendered_validator)


if __name__ == "__main__":
    unittest.main()
