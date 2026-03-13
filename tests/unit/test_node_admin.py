import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toml

from xian.node_admin import (
    apply_snapshot_archive,
    configure_existing_home,
    resolve_seed_nodes,
)
from xian.node_setup import write_toml


class NodeAdminTests(unittest.TestCase):
    def test_resolve_seed_nodes_uses_seed_node_address(self):
        self.assertEqual(
            resolve_seed_nodes(seed_node_address="abc@127.0.0.1"),
            ["abc@127.0.0.1:26656"],
        )

    def test_resolve_seed_nodes_queries_status_endpoint(self):
        with patch(
            "xian.node_admin.fetch_seed_node_status",
            return_value={"result": {"node_info": {"id": "node-123"}}},
        ):
            self.assertEqual(
                resolve_seed_nodes(seed_node="127.0.0.1"),
                ["node-123@127.0.0.1:26656"],
            )

    def test_apply_snapshot_archive_removes_old_dirs_and_extracts_tar(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            (home / "data").mkdir()
            (home / "data" / "old.txt").write_text("old", encoding="utf-8")
            (home / "xian").mkdir()
            (home / "xian" / "old.txt").write_text("old", encoding="utf-8")

            archive_stream = io.BytesIO()
            with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
                payload = b"new state"
                info = tarfile.TarInfo("data/new.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            response = type(
                "Response",
                (),
                {
                    "content": archive_stream.getvalue(),
                    "raise_for_status": staticmethod(lambda: None),
                },
            )()

            with patch("xian.node_admin.requests.get", return_value=response):
                archive_path = apply_snapshot_archive(
                    "https://example.invalid/snapshot.tar.gz",
                    home,
                )

            self.assertTrue((home / "data" / "new.txt").exists())
            self.assertFalse((home / "data" / "old.txt").exists())
            self.assertFalse((home / "xian" / "old.txt").exists())
            self.assertEqual(archive_path, "snapshot.tar.gz")

    def test_configure_existing_home_renders_config_and_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            home = tmp_path / ".cometbft"
            config_path = home / "config" / "config.toml"
            config_path.parent.mkdir(parents=True)

            existing_config = toml.loads(
                """
version = "0.38.7"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "rocksdb"
db_dir = "custom-data"
log_level = "debug"
log_format = "json"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://0.0.0.0:30057"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:30056"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false

[xian]
block_service_mode = false
pruning_enabled = false
blocks_to_keep = 100000
""".strip()
            )
            write_toml(config_path, existing_config)

            configs_dir = tmp_path / "xian-configs"
            legacy_genesis_dir = configs_dir / "legacy" / "genesis"
            legacy_genesis_dir.mkdir(parents=True)
            genesis_payload = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {},
            }
            (legacy_genesis_dir / "genesis-local.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                result = configure_existing_home(
                    home=home,
                    moniker="updated-node",
                    validator_private_key_hex=(
                        "0123456789abcdef0123456789abcdef0123456789abcdef"
                        "0123456789abcdef"
                    ),
                    seed_node_address="seed-1@127.0.0.1",
                    copy_genesis=True,
                    genesis_file_name="genesis-local.json",
                    enable_pruning=True,
                    blocks_to_keep=5000,
                    allow_cors=False,
                )

            rendered_config = toml.load(config_path)
            rendered_validator_key = json.loads(
                (home / "config" / "priv_validator_key.json").read_text(
                    encoding="utf-8"
                )
            )
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )

            self.assertEqual(rendered_config["moniker"], "updated-node")
            self.assertEqual(
                rendered_config["p2p"]["seeds"],
                "seed-1@127.0.0.1:26656",
            )
            self.assertEqual(
                rendered_config["rpc"]["laddr"], "tcp://0.0.0.0:30057"
            )
            self.assertEqual(
                rendered_config["p2p"]["laddr"], "tcp://0.0.0.0:30056"
            )
            self.assertEqual(rendered_config["db_backend"], "rocksdb")
            self.assertEqual(rendered_config["db_dir"], "custom-data")
            self.assertEqual(rendered_config["rpc"]["cors_allowed_origins"], [])
            self.assertTrue(rendered_config["xian"]["pruning_enabled"])
            self.assertEqual(rendered_config["xian"]["blocks_to_keep"], 5000)
            self.assertEqual(rendered_genesis["chain_id"], "xian-local-1")
            self.assertIn("address", rendered_validator_key)
            self.assertEqual(
                result["config_path"],
                str(home / "config" / "config.toml"),
            )
            self.assertEqual(result["seed_nodes"], ["seed-1@127.0.0.1:26656"])


if __name__ == "__main__":
    unittest.main()
