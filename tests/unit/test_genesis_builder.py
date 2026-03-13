import json
import tempfile
import unittest
from pathlib import Path

from xian.genesis_builder import (
    build_genesis_block,
    update_cometbft_genesis,
    write_genesis_block,
)
from xian_py.wallet import Wallet


class GenesisBuilderTests(unittest.TestCase):
    def test_build_genesis_block_uses_importable_contract_bundle(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "seed_contract.s.py").write_text(
                "owner_value = Variable()\n\n"
                "@construct\n"
                "def seed(founder: str):\n"
                "    owner_value.set(founder)\n\n"
                "@export\n"
                "def get_owner():\n"
                "    return owner_value.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "contracts_test.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "seed_contract",
                                "submit_as": "con_seed",
                                "owner": None,
                                "constructor_args": {
                                    "founder": "%%founder_public_key%%"
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            genesis_block = build_genesis_block(
                founder_private_key=founder_private_key,
                network="test",
                contracts_dir=contracts_dir,
            )

        wallet = Wallet(private_key=founder_private_key)
        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }

        self.assertEqual(genesis_block["number"], "0")
        self.assertEqual(genesis_block["origin"]["sender"], wallet.public_key)
        self.assertTrue(genesis_block["origin"]["signature"])
        self.assertIn("con_seed.__code__", state_by_key)
        self.assertEqual(state_by_key["con_seed.owner_value"], wallet.public_key)
        self.assertNotIn("con_seed.__compiled__", state_by_key)

    def test_write_and_update_cometbft_genesis(self):
        abci_genesis = {
            "hash": "abc",
            "number": "7",
            "genesis": [{"key": "con_seed.owner_value", "value": "vk"}],
            "origin": {"sender": "vk", "signature": "sig"},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            genesis_block_path = Path(tmp_dir) / "out" / "genesis_block.json"
            write_genesis_block(genesis_block_path, abci_genesis)
            self.assertEqual(
                json.loads(genesis_block_path.read_text(encoding="utf-8")),
                abci_genesis,
            )

            cometbft_genesis_path = Path(tmp_dir) / "genesis.json"
            cometbft_genesis_path.write_text(
                json.dumps({"chain_id": "xian-local-1", "initial_height": "0"}),
                encoding="utf-8",
            )

            update_cometbft_genesis(
                cometbft_genesis_path,
                abci_genesis=abci_genesis,
            )

            updated = json.loads(
                cometbft_genesis_path.read_text(encoding="utf-8")
            )

        self.assertEqual(updated["abci_genesis"], abci_genesis)
        self.assertEqual(updated["initial_height"], "7")


if __name__ == "__main__":
    unittest.main()
