import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xian.config_paths import (
    resolve_configs_dir,
    resolve_legacy_contracts_dir,
    resolve_legacy_genesis_file,
)


class ConfigPathsTests(unittest.TestCase):
    def test_resolve_configs_dir_prefers_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            configs_dir = Path(tmp_dir) / "xian-configs"
            configs_dir.mkdir()

            self.assertEqual(resolve_configs_dir(configs_dir), configs_dir.resolve())

    def test_resolve_legacy_paths_use_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            configs_dir = Path(tmp_dir) / "xian-configs"
            genesis_dir = configs_dir / "legacy" / "genesis"
            contracts_dir = genesis_dir / "contracts"
            contracts_dir.mkdir(parents=True)
            genesis_file = genesis_dir / "genesis-devnet.json"
            genesis_file.write_text("{}", encoding="utf-8")

            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                self.assertEqual(resolve_configs_dir(), configs_dir.resolve())
                self.assertEqual(
                    resolve_legacy_contracts_dir(), contracts_dir.resolve()
                )
                self.assertEqual(
                    resolve_legacy_genesis_file("genesis-devnet.json"),
                    genesis_file.resolve(),
                )


if __name__ == "__main__":
    unittest.main()
