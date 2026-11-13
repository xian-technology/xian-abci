"""Behaviour pins for reconfiguring an existing home and exporting state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_node_admin import (
    _current_application_archive,
    _signed_snapshot_manifest,
)
from xian_accounts import Ed25519Account

from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
from xian.node_admin import ExistingHomeOptions, configure_existing_home
from xian.node_setup import write_toml
from xian.state_export import build_exported_state
from xian.toml_utils import loads as load_toml_string

_EXISTING_CONFIG = """
version = "0.39.3"
proxy_app = "unix:///tmp/abci.sock"
moniker = "initial-node"
db_backend = "goleveldb"
db_dir = "data"
log_level = "info"
log_format = "plain"
genesis_file = "config/genesis.json"
priv_validator_key_file = "config/priv_validator_key.json"
priv_validator_state_file = "data/priv_validator_state.json"
node_key_file = "config/node_key.json"
abci = "socket"
filter_peers = false

[rpc]
laddr = "tcp://127.0.0.1:26657"
cors_allowed_origins = ["*"]

[p2p]
laddr = "tcp://0.0.0.0:26656"
seeds = ""

[consensus]
create_empty_blocks = false
create_empty_blocks_interval = "0s"

[instrumentation]
prometheus = false
""".strip()


def _initialized_home(root: Path) -> Path:
    home = root / ".cometbft"
    config_path = home / "config" / "config.toml"
    config_path.parent.mkdir(parents=True)
    write_toml(config_path, load_toml_string(_EXISTING_CONFIG))
    return home


class ConfigureExistingHomeRules(unittest.TestCase):
    def test_keyword_call_without_moniker_or_options_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = _initialized_home(Path(tmp_dir))
            with self.assertRaisesRegex(TypeError, "moniker is required"):
                configure_existing_home(home=home)

    def test_without_genesis_or_validator_key_writes_neither(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = _initialized_home(Path(tmp_dir))

            result = configure_existing_home(home=home, moniker="node")

            self.assertIsNone(result["genesis_path"])
            self.assertIsNone(result["priv_validator_key_path"])
            self.assertIsNone(result["snapshot_archive_name"])
            self.assertFalse((home / "config" / "genesis.json").exists())
            self.assertFalse((home / "config" / "priv_validator_key.json").exists())

    def test_genesis_payload_wins_over_genesis_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = _initialized_home(root)
            source = root / "source-genesis.json"
            source.write_text(json.dumps({"chain_id": "from-source"}), encoding="utf-8")

            configure_existing_home(
                options=ExistingHomeOptions(
                    moniker="node",
                    home=home,
                    genesis_source=str(source),
                    genesis_payload={"chain_id": "from-payload"},
                )
            )

            written = json.loads((home / "config" / "genesis.json").read_text(encoding="utf-8"))
            self.assertEqual(written, {"chain_id": "from-payload"})

    def _configure_from_snapshot(self, home: Path, *, trusted_key: str, chain_id: str):
        archive_bytes = _current_application_archive()
        manifest, _ = _signed_snapshot_manifest(
            archive_url="https://example.invalid/snapshot.tar.gz",
            archive_bytes=archive_bytes,
            signing_account=self.signer,
        )
        with (
            patch("xian.node_admin._fetch_json_url", return_value=manifest),
            patch("xian.node_admin._download_binary_url", return_value=archive_bytes),
        ):
            return configure_existing_home(
                options=ExistingHomeOptions(
                    moniker="node",
                    home=home,
                    snapshot_url="https://example.invalid/snapshot-manifest.json",
                    snapshot_signing_public_keys=(trusted_key,),
                    snapshot_expected_chain_id=chain_id,
                )
            )

    def setUp(self):
        self.signer = Ed25519Account.generate()

    def test_snapshot_url_restores_the_signed_archive(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = _initialized_home(Path(tmp_dir))

            result = self._configure_from_snapshot(
                home, trusted_key=self.signer.public_key, chain_id="xian-test-1"
            )

            self.assertEqual(result["snapshot_archive_name"], "snapshot.tar.gz")
            self.assertTrue((home / "data" / "new.txt").exists())

    def test_snapshot_signer_must_be_one_of_the_given_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = _initialized_home(Path(tmp_dir))
            stranger = Ed25519Account.generate().public_key

            with self.assertRaises(ValueError):
                self._configure_from_snapshot(home, trusted_key=stranger, chain_id="xian-test-1")
            self.assertFalse((home / "data" / "new.txt").exists())

    def test_snapshot_chain_id_must_match_the_expected_one(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = _initialized_home(Path(tmp_dir))

            with self.assertRaises(ValueError):
                self._configure_from_snapshot(
                    home, trusted_key=self.signer.public_key, chain_id="other-chain-1"
                )
            self.assertFalse((home / "data" / "new.txt").exists())


class BuildExportedStateRules(unittest.TestCase):
    def _export(self, latest_block_hash: bytes):
        return build_exported_state(
            founder_private_key=None,
            contract_state={"con_a.value": 1},
            run_state={EXECUTION_POLICY_KEY: current_execution_policy()},
            latest_block_hash=latest_block_hash,
            latest_block_height=3,
            latest_block_nanos=5,
        )

    def test_export_matching_the_latest_app_hash_is_accepted(self):
        expected = self._export(b"")
        exported = self._export(bytes.fromhex(expected["hash"]))

        self.assertEqual(exported["hash"], expected["hash"])
        self.assertEqual((exported["number"], exported["nanos"]), (3, 5))
        self.assertEqual(exported["origin"], {"signature": "", "sender": ""})

    def test_export_that_disagrees_with_the_latest_app_hash_is_refused(self):
        with self.assertRaisesRegex(ValueError, "does not match latest block app hash"):
            self._export(b"\x01" * 32)


if __name__ == "__main__":
    unittest.main()
