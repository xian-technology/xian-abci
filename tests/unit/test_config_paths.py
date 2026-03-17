import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.config_paths import (
    resolve_configs_dir,
    resolve_contracts_dir,
    resolve_genesis_source,
    resolve_network_genesis_file,
)


class ConfigPathsTests(unittest.TestCase):
    def test_resolve_configs_dir_prefers_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            configs_dir = Path(tmp_dir) / "xian-configs"
            configs_dir.mkdir()

            self.assertEqual(resolve_configs_dir(configs_dir), configs_dir.resolve())

    def test_resolve_contracts_dir_uses_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            configs_dir = Path(tmp_dir) / "xian-configs"
            contracts_dir = configs_dir / "contracts"
            contracts_dir.mkdir(parents=True)

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                self.assertEqual(resolve_configs_dir(), configs_dir.resolve())
                self.assertEqual(
                    resolve_contracts_dir(), contracts_dir.resolve()
                )

    def test_resolve_genesis_source_prefers_network_first_layout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            configs_dir = Path(tmp_dir) / "xian-configs"
            network_dir = configs_dir / "networks" / "mainnet"
            network_dir.mkdir(parents=True)
            genesis_file = network_dir / "genesis.json"
            genesis_file.write_text("{}", encoding="utf-8")

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                self.assertEqual(
                    resolve_network_genesis_file("mainnet"),
                    genesis_file.resolve(),
                )
                self.assertEqual(
                    resolve_genesis_source("mainnet"),
                    genesis_file.resolve(),
                )
                self.assertEqual(
                    resolve_genesis_source("networks/mainnet/genesis.json"),
                    genesis_file.resolve(),
                )


if __name__ == "__main__":
    unittest.main()
