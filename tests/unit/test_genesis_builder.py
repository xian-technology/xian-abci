import json
import tempfile
import unittest
from pathlib import Path

from xian_py.wallet import Wallet

from xian.genesis_builder import (
    build_cometbft_genesis,
    build_genesis_block,
    build_local_network_genesis,
    build_single_validator_genesis,
    build_validator_genesis_entry,
    update_cometbft_genesis,
    write_genesis_block,
)


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
        self.assertEqual(
            state_by_key["con_seed.owner_value"], wallet.public_key
        )
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

    def test_build_genesis_block_applies_constructor_overrides(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "foundation.s.py").write_text(
                "foundation_vk = Variable()\n\n"
                "@construct\n"
                "def seed(vk: str):\n"
                "    foundation_vk.set(vk)\n\n"
                "@export\n"
                "def get_vk():\n"
                "    return foundation_vk.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "members.s.py").write_text(
                "nodes = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    nodes.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return nodes.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "contracts_local.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "foundation",
                                "owner": None,
                                "constructor_args": {"vk": "old-vk"},
                            },
                            {
                                "name": "members",
                                "submit_as": "masternodes",
                                "owner": None,
                                "constructor_args": {
                                    "genesis_nodes": ["old-node"],
                                    "genesis_registration_fee": 1,
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            genesis_block = build_genesis_block(
                founder_private_key=founder_private_key,
                network="local",
                contracts_dir=contracts_dir,
                constructor_overrides={
                    "foundation": {"vk": "new-vk"},
                    "masternodes": {
                        "genesis_nodes": ["node-a"],
                        "genesis_registration_fee": 123,
                    },
                },
            )

        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }
        self.assertEqual(state_by_key["foundation.foundation_vk"], "new-vk")
        self.assertEqual(state_by_key["masternodes.nodes"], ["node-a"])
        self.assertEqual(state_by_key["masternodes.fee"], 123)

    def test_build_cometbft_genesis_includes_validator_entry(self):
        abci_genesis = {
            "hash": "abc",
            "number": "3",
            "genesis": [],
            "origin": {"sender": "vk", "signature": "sig"},
        }
        validator = build_validator_genesis_entry(
            priv_validator_key={
                "address": "ABC",
                "pub_key": {
                    "type": "tendermint/PubKeyEd25519",
                    "value": "pub",
                },
            },
            power=15,
            name="validator-1",
        )
        genesis = build_cometbft_genesis(
            chain_id="xian-local-1",
            abci_genesis=abci_genesis,
            validators=[validator],
            genesis_time="2026-03-13T12:00:00.000000Z",
        )

        self.assertEqual(genesis["chain_id"], "xian-local-1")
        self.assertEqual(genesis["initial_height"], "3")
        self.assertEqual(genesis["validators"][0]["power"], "15")
        self.assertEqual(genesis["validators"][0]["name"], "validator-1")
        self.assertEqual(genesis["abci_genesis"], abci_genesis)

    def test_build_single_validator_genesis_uses_founder_and_validator_inputs(
        self,
    ):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        wallet = Wallet(private_key=founder_private_key)

        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "currency.s.py").write_text(
                "currency_vk = Variable()\n\n"
                "@construct\n"
                "def seed(vk: str):\n"
                "    currency_vk.set(vk)\n\n"
                "@export\n"
                "def get_vk():\n"
                "    return currency_vk.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "foundation.s.py").write_text(
                "foundation_vk = Variable()\n\n"
                "@construct\n"
                "def seed(vk: str):\n"
                "    foundation_vk.set(vk)\n\n"
                "@export\n"
                "def get_vk():\n"
                "    return foundation_vk.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "members.s.py").write_text(
                "nodes = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    nodes.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return nodes.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "contracts_local.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "currency",
                                "owner": None,
                                "constructor_args": {"vk": "old-vk"},
                            },
                            {
                                "name": "foundation",
                                "owner": None,
                                "constructor_args": {"vk": "old-vk"},
                            },
                            {
                                "name": "members",
                                "submit_as": "masternodes",
                                "owner": None,
                                "constructor_args": {
                                    "genesis_nodes": ["old-node"],
                                    "genesis_registration_fee": 1,
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            genesis = build_single_validator_genesis(
                chain_id="xian-local-1",
                priv_validator_key={
                    "address": "ABC",
                    "pub_key": {
                        "type": "tendermint/PubKeyEd25519",
                        "value": "pub",
                    },
                    "priv_key": {
                        "type": "tendermint/PrivKeyEd25519",
                        "value": "priv",
                    },
                },
                founder_private_key=founder_private_key,
                network="local",
                validator_name="validator-1",
                validator_power=25,
                registration_fee=321,
                contracts_dir=contracts_dir,
            )

        state_by_key = {
            entry["key"]: entry["value"]
            for entry in genesis["abci_genesis"]["genesis"]
        }
        self.assertEqual(
            state_by_key["currency.currency_vk"], wallet.public_key
        )
        self.assertEqual(
            state_by_key["foundation.foundation_vk"], wallet.public_key
        )
        self.assertEqual(state_by_key["masternodes.nodes"], [wallet.public_key])
        self.assertEqual(state_by_key["masternodes.fee"], 321)
        self.assertEqual(genesis["validators"][0]["power"], "25")
        self.assertEqual(genesis["validators"][0]["name"], "validator-1")

    def test_build_local_network_genesis_supports_multiple_validators(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        validator_two_private_key = (
            "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        founder_wallet = Wallet(private_key=founder_private_key)
        validator_two_wallet = Wallet(private_key=validator_two_private_key)

        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "currency.s.py").write_text(
                "currency_vk = Variable()\n\n"
                "@construct\n"
                "def seed(vk: str):\n"
                "    currency_vk.set(vk)\n\n"
                "@export\n"
                "def get_vk():\n"
                "    return currency_vk.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "foundation.s.py").write_text(
                "foundation_vk = Variable()\n\n"
                "@construct\n"
                "def seed(vk: str):\n"
                "    foundation_vk.set(vk)\n\n"
                "@export\n"
                "def get_vk():\n"
                "    return foundation_vk.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "members.s.py").write_text(
                "nodes = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    nodes.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return nodes.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "contracts_local.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "currency",
                                "owner": None,
                                "constructor_args": {"vk": "old-vk"},
                            },
                            {
                                "name": "foundation",
                                "owner": None,
                                "constructor_args": {"vk": "old-vk"},
                            },
                            {
                                "name": "members",
                                "submit_as": "masternodes",
                                "owner": None,
                                "constructor_args": {
                                    "genesis_nodes": ["old-node"],
                                    "genesis_registration_fee": 1,
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            genesis = build_local_network_genesis(
                chain_id="xian-local-1",
                founder_private_key=founder_private_key,
                validators=[
                    {
                        "account_public_key": founder_wallet.public_key,
                        "name": "validator-1",
                        "power": 10,
                        "priv_validator_key": {
                            "address": "ABC",
                            "pub_key": {
                                "type": "tendermint/PubKeyEd25519",
                                "value": "pub-1",
                            },
                            "priv_key": {
                                "type": "tendermint/PrivKeyEd25519",
                                "value": "priv-1",
                            },
                        },
                    },
                    {
                        "account_public_key": validator_two_wallet.public_key,
                        "name": "validator-2",
                        "power": 25,
                        "priv_validator_key": {
                            "address": "DEF",
                            "pub_key": {
                                "type": "tendermint/PubKeyEd25519",
                                "value": "pub-2",
                            },
                            "priv_key": {
                                "type": "tendermint/PrivKeyEd25519",
                                "value": "priv-2",
                            },
                        },
                    },
                ],
                network="local",
                registration_fee=321,
                contracts_dir=contracts_dir,
            )

        state_by_key = {
            entry["key"]: entry["value"]
            for entry in genesis["abci_genesis"]["genesis"]
        }
        self.assertEqual(state_by_key["currency.currency_vk"], founder_wallet.public_key)
        self.assertEqual(
            state_by_key["foundation.foundation_vk"], founder_wallet.public_key
        )
        self.assertEqual(
            state_by_key["masternodes.nodes"],
            [founder_wallet.public_key, validator_two_wallet.public_key],
        )
        self.assertEqual(state_by_key["masternodes.fee"], 321)
        self.assertEqual(len(genesis["validators"]), 2)
        self.assertEqual(genesis["validators"][1]["name"], "validator-2")
        self.assertEqual(genesis["validators"][1]["power"], "25")


if __name__ == "__main__":
    unittest.main()
