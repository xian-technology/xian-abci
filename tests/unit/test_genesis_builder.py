import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contracting.artifacts import build_contract_artifacts
from contracting.local import ContractingClient
from contracting.runtime_features import runtime_feature_state_key
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

from xian.execution_engine import build_vm_runtime, execute_vm_contract
from xian.genesis_builder import (
    build_bundle_network_genesis,
    build_cometbft_genesis,
    build_genesis_block,
    build_local_network_genesis,
    build_single_validator_genesis,
    build_validator_genesis_entry,
    build_validator_genesis_entry_from_public_key,
    derive_genesis_validators_from_bundle,
    update_cometbft_genesis,
    write_genesis_block,
)
from xian.nonce import NonceStorage
from xian.state_root import StateRootCache
from xian.utils.block import store_genesis_block


class GenesisBuilderTests(unittest.TestCase):
    def test_local_contract_bundle_seeds_governed_zk_registry(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        contracts_dir = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
        )

        genesis_block = build_genesis_block(
            founder_private_key=founder_private_key,
            network="local",
            contracts_dir=contracts_dir,
        )

        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }
        self.assertIn("zk_registry.__source__", state_by_key)
        self.assertIn("zk_registry.__xian_ir_v1__", state_by_key)
        self.assertEqual(state_by_key["zk_registry.registry_owner"], "governance")
        self.assertEqual(state_by_key[runtime_feature_state_key("zk")], False)

    def test_genesis_hash_matches_serialized_import_state_root(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        contracts_dir = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
        )

        async def build_imported_root(genesis_block):
            loaded_genesis = json.loads(encode(genesis_block))
            with tempfile.TemporaryDirectory() as tmp_dir:
                client = ContractingClient(storage_home=Path(tmp_dir))
                nonce_storage = NonceStorage(client)
                client.raw_driver.flush_full()
                await store_genesis_block(
                    client,
                    nonce_storage,
                    loaded_genesis,
                )
                return StateRootCache.from_driver(
                    client.raw_driver
                ).root_hash.hex()

        genesis_block = build_genesis_block(
            founder_private_key=founder_private_key,
            network="local",
            contracts_dir=contracts_dir,
        )

        self.assertEqual(
            genesis_block["hash"],
            asyncio.run(build_imported_root(genesis_block)),
        )

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

        wallet = Ed25519Account(founder_private_key)
        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }

        self.assertEqual(genesis_block["number"], "0")
        self.assertEqual(genesis_block["origin"]["sender"], wallet.public_key)
        self.assertTrue(genesis_block["origin"]["signature"])
        self.assertIn("submission.__source__", state_by_key)
        self.assertIn("submission.__xian_ir_v1__", state_by_key)
        self.assertIn("con_seed.__source__", state_by_key)
        self.assertIn("con_seed.__xian_ir_v1__", state_by_key)
        self.assertEqual(
            state_by_key["con_seed.owner_value"], wallet.public_key
        )

    def test_genesis_contract_deploy_matches_native_submission_path(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        fixed_now = Datetime(2026, 4, 12, 12, 0)
        source = (
            "owner_value = Variable()\n"
            "limits = Variable()\n"
            "ratios = Variable()\n\n"
            "@construct\n"
            "def seed(founder: str, limits_arg: list[int], ratios_arg: list[float]):\n"
            "    owner_value.set(founder)\n"
            "    limits.set(limits_arg)\n"
            "    ratios.set(ratios_arg)\n\n"
            "@export\n"
            "def get_owner():\n"
            "    return owner_value.get()\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "seed_contract.s.py").write_text(
                source,
                encoding="utf-8",
            )
            (contracts_dir / "contracts_test.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "seed_contract",
                                "submit_as": "con_seed_native_probe",
                                "owner": "owner-vk",
                                "constructor_args": {
                                    "founder": "%%founder_public_key%%",
                                    "limits_arg": [1, 2, 3],
                                    "ratios_arg": [],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "xian.genesis_builder._genesis_submission_time",
                return_value=fixed_now,
            ), mock.patch(
                "xian.genesis_builder.execute_vm_contract",
                wraps=execute_vm_contract,
            ) as native_execute:
                genesis_block = build_genesis_block(
                    founder_private_key=founder_private_key,
                    network="test",
                    contracts_dir=contracts_dir,
                )
            self.assertEqual(native_execute.call_count, 1)
            self.assertEqual(
                native_execute.call_args.kwargs["contract_name"],
                "submission",
            )
            self.assertEqual(
                native_execute.call_args.kwargs["function_name"],
                "submit_contract",
            )

            native_driver = Driver(storage_home=Path(tmp_dir) / "native")
            native_driver.flush_full()
            ContractingClient(driver=native_driver)
            artifacts = build_contract_artifacts(
                module_name="con_seed_native_probe",
                source=source,
                lint=True,
                vm_profile="xian_vm_v1",
            )
            native_output = execute_vm_contract(
                build_vm_runtime(),
                native_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": "con_seed_native_probe",
                    "deployment_artifacts": artifacts,
                    "owner": "owner-vk",
                    "constructor_args": {
                        "founder": Ed25519Account(founder_private_key).public_key,
                        "limits_arg": [1, 2, 3],
                        "ratios_arg": [],
                    },
                },
                environment={
                    "now": fixed_now,
                    "block_num": 0,
                    "block_hash": "",
                    "chain_id": "genesis-test",
                },
                meter=False,
            )
            self.assertEqual(native_output.status_code, 0)
            native_driver.apply_writes(native_output.writes)
            native_driver.hard_apply("0")
            native_driver.flush_cache()

        genesis_state = {
            entry["key"]: entry["value"]
            for entry in genesis_block["genesis"]
            if entry["key"].startswith("con_seed_native_probe.")
        }
        native_state = native_driver.items(prefix="con_seed_native_probe.")

        self.assertEqual(genesis_state, native_state)

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
        self.assertEqual(updated["app_hash"], "abc")

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
            (contracts_dir / "validators.s.py").write_text(
                "active_validators = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    active_validators.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return active_validators.get()\n",
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
                                "name": "validators",
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
                    "validators": {
                        "genesis_nodes": ["node-a"],
                        "genesis_registration_fee": 123,
                    },
                },
            )

        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }
        self.assertEqual(state_by_key["foundation.foundation_vk"], "new-vk")
        self.assertEqual(state_by_key["validators.active_validators"], ["node-a"])
        self.assertEqual(state_by_key["validators.fee"], 123)

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
        self.assertEqual(genesis["app_hash"], "abc")
        self.assertEqual(genesis["validators"][0]["power"], "15")
        self.assertEqual(genesis["validators"][0]["name"], "validator-1")
        self.assertEqual(genesis["abci_genesis"], abci_genesis)

    def test_build_validator_genesis_entry_from_public_key(self):
        validator = build_validator_genesis_entry_from_public_key(
            public_key_hex=(
                "ee06a34cf08bf72ce592d26d36b90c79"
                "daba2829ba9634992d034318160d49f9"
            ),
            power=15,
        )

        self.assertEqual(
            validator["address"], "8ABB2DE01AE15C5F4EBD0D4455C6BCD9047B7CBB"
        )
        self.assertEqual(validator["power"], "15")
        self.assertEqual(
            validator["pub_key"]["value"],
            "7gajTPCL9yzlktJtNrkMedq6KCm6ljSZLQNDGBYNSfk=",
        )

    def test_derive_genesis_validators_from_bundle(self):
        contracts_dir = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
        )

        validators = derive_genesis_validators_from_bundle(
            network="devnet",
            contracts_dir=contracts_dir,
        )

        self.assertEqual(len(validators), 2)
        self.assertEqual(
            validators[0]["address"], "614EBE42CBE8354F733851F4316D0DE316B1AEF0"
        )
        self.assertEqual(
            validators[1]["address"], "5C7B6531A88A6C6941A7AA495AF041394F8345F0"
        )

    def test_derive_genesis_validators_from_bundle_uses_declared_powers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            contracts_dir = Path(tmp_dir) / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "contracts_test.json").write_text(
                json.dumps(
                    {
                        "extension": ".s.py",
                        "contracts": [
                            {
                                "name": "validators",
                                "owner": None,
                                "constructor_args": {
                                    "genesis_nodes": [
                                        "ee06a34cf08bf72ce592d26d36b90c79"
                                        "daba2829ba9634992d034318160d49f9",
                                        "7fa496ca2438e487cc45a8a27fd95b2e"
                                        "fe373223f7b72868fbab205d686be48e",
                                    ],
                                    "default_node_power": 17,
                                    "genesis_powers": {
                                        "7fa496ca2438e487cc45a8a27fd95b2e"
                                        "fe373223f7b72868fbab205d686be48e": 23
                                    },
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            validators = derive_genesis_validators_from_bundle(
                network="test",
                contracts_dir=contracts_dir,
            )

        self.assertEqual(validators[0]["power"], "17")
        self.assertEqual(validators[1]["power"], "23")

    def test_build_bundle_network_genesis_uses_bundle_seed_validators(self):
        contracts_dir = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
        )

        genesis = build_bundle_network_genesis(
            chain_id="xian-devnet-1",
            network="devnet",
            contracts_dir=contracts_dir,
            genesis_time="2026-03-30T00:00:00.000000Z",
        )

        self.assertEqual(genesis["chain_id"], "xian-devnet-1")
        self.assertEqual(
            genesis["genesis_time"], "2026-03-30T00:00:00.000000Z"
        )
        self.assertEqual(
            genesis["abci_genesis"]["origin"],
            {"sender": "", "signature": ""},
        )
        self.assertEqual(len(genesis["validators"]), 2)
        self.assertEqual(
            genesis["validators"][0]["address"],
            "614EBE42CBE8354F733851F4316D0DE316B1AEF0",
        )

    def test_local_bundle_pins_validators_policy_in_genesis(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        contracts_dir = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
        )

        genesis_block = build_genesis_block(
            founder_private_key=founder_private_key,
            network="local",
            contracts_dir=contracts_dir,
        )

        state_by_key = {
            entry["key"]: entry["value"] for entry in genesis_block["genesis"]
        }

        self.assertEqual(
            state_by_key["validators.config:selection_mode"], "manual"
        )
        self.assertEqual(state_by_key["validators.config:max_validators"], 5)
        self.assertEqual(
            state_by_key["validators.config:max_commission_bps"], 10000
        )
        self.assertEqual(
            state_by_key["validators.config:slash_destination"], "dao"
        )
        self.assertEqual(
            state_by_key["validators.powers:ee06a34cf08bf72ce592d26d36b90c79daba2829ba9634992d034318160d49f9"],
            10,
        )

    def test_build_single_validator_genesis_uses_founder_and_validator_inputs(
        self,
    ):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        wallet = Ed25519Account(founder_private_key)

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
            (contracts_dir / "validators.s.py").write_text(
                "active_validators = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    active_validators.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return active_validators.get()\n",
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
                                "name": "validators",
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
        self.assertEqual(state_by_key["validators.active_validators"], [wallet.public_key])
        self.assertEqual(state_by_key["validators.fee"], 321)
        self.assertEqual(genesis["validators"][0]["power"], "25")
        self.assertEqual(genesis["validators"][0]["name"], "validator-1")

    def test_build_local_network_genesis_supports_multiple_validators(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        validator_two_private_key = (
            "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        founder_wallet = Ed25519Account(founder_private_key)
        validator_two_wallet = Ed25519Account(validator_two_private_key)

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
            (contracts_dir / "validators.s.py").write_text(
                "active_validators = Variable()\nfee = Variable()\n\n"
                "@construct\n"
                "def seed(genesis_nodes: list, genesis_registration_fee: int):\n"
                "    active_validators.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return active_validators.get()\n",
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
                                "name": "validators",
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
            state_by_key["validators.active_validators"],
            [founder_wallet.public_key, validator_two_wallet.public_key],
        )
        self.assertEqual(state_by_key["validators.fee"], 321)
        self.assertEqual(len(genesis["validators"]), 2)
        self.assertEqual(genesis["validators"][1]["name"], "validator-2")
        self.assertEqual(genesis["validators"][1]["power"], "25")

    def test_build_local_network_genesis_overrides_bundle_validator_metadata(self):
        founder_private_key = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        validator_two_private_key = (
            "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        founder_wallet = Ed25519Account(founder_private_key)
        validator_two_wallet = Ed25519Account(validator_two_private_key)

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
            (contracts_dir / "validators.s.py").write_text(
                "active_validators = Variable()\n"
                "fee = Variable()\n"
                "powers = Hash()\n"
                "reward_keys = Hash()\n\n"
                "@construct\n"
                "def seed(\n"
                "    genesis_nodes: list,\n"
                "    genesis_registration_fee: int,\n"
                "    genesis_powers: dict = None,\n"
                "    genesis_reward_keys: dict = None,\n"
                "):\n"
                "    active_validators.set(genesis_nodes)\n"
                "    fee.set(genesis_registration_fee)\n"
                "    for node in genesis_nodes:\n"
                "        powers[node] = genesis_powers[node]\n"
                "        reward_keys[node] = genesis_reward_keys[node]\n\n"
                "@export\n"
                "def get_nodes():\n"
                "    return active_validators.get()\n",
                encoding="utf-8",
            )
            (contracts_dir / "contracts_testnet.json").write_text(
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
                                "name": "validators",
                                "owner": None,
                                "constructor_args": {
                                    "genesis_nodes": ["old-node"],
                                    "genesis_powers": {"old-node": 99},
                                    "genesis_reward_keys": {
                                        "old-node": "old-reward-key"
                                    },
                                    "genesis_registration_fee": 1,
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            genesis = build_local_network_genesis(
                chain_id="xian-testnet-local-1",
                founder_private_key=founder_private_key,
                validators=[
                    {
                        "account_public_key": founder_wallet.public_key,
                        "name": "validator-1",
                        "power": 12,
                        "reward_key": "reward-key-1",
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
                        "power": 18,
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
                network="testnet",
                registration_fee=321,
                contracts_dir=contracts_dir,
            )

        state_by_key = {
            entry["key"]: entry["value"]
            for entry in genesis["abci_genesis"]["genesis"]
        }
        self.assertEqual(
            state_by_key["validators.active_validators"],
            [founder_wallet.public_key, validator_two_wallet.public_key],
        )
        self.assertEqual(
            state_by_key[f"validators.powers:{founder_wallet.public_key}"],
            12,
        )
        self.assertEqual(
            state_by_key[f"validators.powers:{validator_two_wallet.public_key}"],
            18,
        )
        self.assertEqual(
            state_by_key[f"validators.reward_keys:{founder_wallet.public_key}"],
            "reward-key-1",
        )
        self.assertEqual(
            state_by_key[
                f"validators.reward_keys:{validator_two_wallet.public_key}"
            ],
            validator_two_wallet.public_key,
        )
        self.assertEqual(genesis["validators"][0]["power"], "12")
        self.assertEqual(genesis["validators"][1]["power"], "18")


if __name__ == "__main__":
    unittest.main()
