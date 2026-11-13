"""Node file writers create missing parent directories and refuse overwrites."""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from xian.node_setup import (
    NodeConfigOptions,
    build_priv_validator_key,
    materialize_cometbft_home,
    render_cometbft_config,
    render_xian_config,
    write_json,
    write_toml,
)
from xian.toml_utils import load as load_toml


class NodeFileWriterRules(unittest.TestCase):
    def test_public_and_private_writes_create_missing_parents(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            public = Path(tmp_dir) / "a" / "b" / "public.toml"
            private = Path(tmp_dir) / "c" / "d" / "private.json"

            write_toml(public, {"key": "value"})
            write_json(private, {"secret": 1}, private=True)

            self.assertEqual(load_toml(public), {"key": "value"})
            self.assertEqual(json.loads(private.read_text(encoding="utf-8")), {"secret": 1})
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_existing_file_is_kept_unless_overwrite_is_set(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "x.json"
            write_json(path, {"v": 1}, private=True)

            with self.assertRaises(FileExistsError):
                write_json(path, {"v": 2}, private=True)
            write_json(path, {"v": 3}, overwrite=True, private=True)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 3})

    def test_materialize_home_creates_a_home_whose_parents_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "nodes" / "validator-1" / ".cometbft"
            options = NodeConfigOptions(moniker="validator-1")

            result = materialize_cometbft_home(
                home=home,
                config=render_cometbft_config(options=options),
                xian_config=render_xian_config(options=options),
                genesis={"chain_id": "xian-local-1", "validators": [], "abci_genesis": {}},
                priv_validator_key=build_priv_validator_key("01" * 32),
            )

            for name in ("config_path", "genesis_path", "priv_validator_key_path"):
                self.assertTrue(Path(result[name]).is_file(), name)
            self.assertEqual(stat.S_IMODE((home / "config").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((home / "data").stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
