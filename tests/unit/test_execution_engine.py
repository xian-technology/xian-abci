import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from contracting.local import ContractingClient
from contracting.runtime_features import (
    RUNTIME_FEATURE_ZK,
    runtime_feature_without_state,
    set_driver_runtime_features,
)
from contracting.storage.driver import Driver
from xian_accounts import Ed25519Account
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime
from xian_vm_core import execute_tx

from xian.execution_engine import (
    _load_vm_runtime_bindings,
    build_vm_runtime,
    vm_call_context,
)
from xian.execution_policy import EXECUTION_POLICY_KEY
from xian.execution_policy import TX_ADMISSION_REVISION, VM_EXECUTION_REVISION
from xian.fee_policy import TxFeePolicy
from xian.nonce import NonceStorage
from xian.processor import TxProcessor
from xian.utils.tx import (
    canonical_json,
    transaction_bytes,
    tx_hash_from_tx,
    validate_transaction_after_static,
)


def run_call(
    driver,
    *,
    sender,
    contract_name,
    function_name,
    kwargs,
    environment,
    meter,
    chi_budget=0,
    transaction_size_bytes=0,
    charge_fees=False,
    fork_values=None,
):
    """One call by the block executor's rules on a fork of the driver's
    state (with ``fork_values`` written into the fork), without fees unless
    asked; the effects are returned, never staged."""
    call = {
        "sender": sender,
        "contract_name": contract_name,
        "function_name": function_name,
        "kwargs": kwargs,
    }
    storage, overlay = driver.committed_storage()
    fork = overlay.snapshot()
    fork.write_values(list((fork_values or {}).items()))
    return execute_tx(
        storage,
        fork,
        **call,
        context=vm_call_context(**call, environment=environment),
        meter=meter,
        charge_fees=charge_fees,
        chi_budget=chi_budget,
        transaction_size_bytes=transaction_size_bytes,
        protected_keys=(EXECUTION_POLICY_KEY,),
        zk_enabled_without_state=runtime_feature_without_state(driver, RUNTIME_FEATURE_ZK),
    )


def _process(processor, tx):
    """The response of a paid transaction alone in a block."""
    [response] = processor.process_block(
        [tx], [transaction_bytes(tx)], fee_policy=TxFeePolicy.paid_metered()
    )
    return response


def run_paid(driver, *, chi_budget, transaction_size_bytes, **call):
    """One paid call by the block executor's rules (fee reserved, refunded
    and charged at a chi rate of 20) on a fork of the driver's state:
    ``output`` is the run, ``writes`` its effects with the fee."""
    run = run_call(
        driver,
        meter=True,
        chi_budget=chi_budget,
        transaction_size_bytes=transaction_size_bytes,
        charge_fees=True,
        fork_values={"chi_cost.S:value": 20},
        **call,
    )
    return types.SimpleNamespace(output=run, writes=run.writes, chi_used=run.chi_used)


class ExecutionEngineRuntimeTests(unittest.TestCase):
    def setUp(self):
        _load_vm_runtime_bindings.cache_clear()

    def test_native_transactions_reject_private_functions_and_constructor_entrypoints(self):
        source = """counter = Variable()

@construct
def seed():
    counter.set(7)

@export
def bump():
    counter.set(counter.get() + 1)
    return counter.get()

def hidden():
    counter.set(999)
    return 'private entry executed'
"""
        account = Ed25519Account("11" * 32)
        policy = TxFeePolicy.free_metered()
        for function in ("hidden", "seed"):
            with (
                self.subTest(function=function),
                tempfile.TemporaryDirectory(prefix="native-entry-", dir=Path.home()) as home,
            ):
                with ContractingClient(storage_home=home, metering=False) as client:
                    client.submit(source, name="con_private_node", signer=account.public_key)
                    contract = client.get_contract_proxy("con_private_node")
                    self.assertEqual(contract.bump(signer=account.public_key), 8)
                    client.raw_driver.commit()
                    payload = {
                        "sender": account.public_key,
                        "contract": "con_private_node",
                        "function": function,
                        "kwargs": {},
                        "nonce": 0,
                        "chi_supplied": 10_000,
                        "chain_id": "xian-local",
                    }
                    tx = {
                        "payload": payload,
                        "metadata": {"signature": account.sign_msg(canonical_json(payload))},
                    }
                    validate_transaction_after_static(
                        client,
                        NonceStorage(client),
                        tx,
                        tx_hash=tx_hash_from_tx(tx),
                        fee_policy=policy,
                    )
                    tx["b_meta"] = {
                        "nanos": 1_000_000_000,
                        "hash": "ab" * 32,
                        "height": 1,
                        "chain_id": "xian-local",
                    }
                    [result] = TxProcessor(client=client).process_block(
                        [tx], [transaction_bytes(tx)], fee_policy=policy
                    )
                    self.assertIsNotNone(result["tx_result"])
                    self.assertEqual(result["tx_result"]["status"], 1)
                    self.assertEqual(result["tx_result"]["state"], [])
                    self.assertEqual(result["tx_result"]["events"], [])
                    self.assertEqual(client.raw_driver.get("con_private_node.counter"), 8)
                    self.assertEqual(contract.bump(signer=account.public_key), 9)

    def test_build_runtime_for_vm_requires_native_package(self):
        with mock.patch.dict(sys.modules, {"xian_vm_core": None}):
            with self.assertRaisesRegex(ValueError, "xian-tech-vm-core"):
                build_vm_runtime()

    def test_build_runtime_for_vm_requires_supported_native_constants(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda _profile: {
                "vm_profile": "xian_vm_v2",
                "execution_revision": VM_EXECUTION_REVISION,
                "set_codec": "xian-set-1",
                "tx_admission_revision": TX_ADMISSION_REVISION,
            },
            supports_execution_policy=lambda *_args: False,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            with self.assertRaisesRegex(ValueError, "does not support"):
                build_vm_runtime()

    def test_build_runtime_for_vm_records_runtime_info(self):
        fake_bindings = types.SimpleNamespace(
            runtime_info=lambda _profile: {
                "vm_profile": "xian_vm_v2",
                "host_catalog_version": "xian_vm_v2_host_v1",
                "execution_revision": VM_EXECUTION_REVISION,
                "set_codec": "xian-set-1",
                "tx_admission_revision": TX_ADMISSION_REVISION,
            },
            supports_execution_policy=lambda *_args: True,
        )

        with mock.patch(
            "xian.execution_engine._load_vm_runtime_bindings",
            return_value=fake_bindings,
        ):
            runtime = build_vm_runtime()

        self.assertEqual(runtime.runtime_info["vm_profile"], "xian_vm_v2")
        self.assertEqual(runtime.mode, "xian_vm_v2")

    def test_execute_native_submission_deploy_stages_contract_artifacts(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        code = (
            "v = Variable()\n\n"
            "@construct\n"
            "def seed(value: int = 1):\n"
            "    v.set(value)\n\n"
            "@export\n"
            "def ping():\n"
            "    return v.get()\n"
        )
        output = run_call(
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_native_submission_probe",
                "code": code,
                "constructor_args": {"value": 9},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=True,
            chi_budget=10_000,
            transaction_size_bytes=256,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_native_submission_probe.__source__", output.writes)
        self.assertIn("con_native_submission_probe.__xian_ir_v1__", output.writes)
        self.assertEqual(output.writes["con_native_submission_probe.v"], 9)
        self.assertTrue(any(event.get("event") == "ContractDeployed" for event in output.events))
        self.assertGreater(output.chi_used, 0)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_execute_native_submission_rejects_submitted_ir_artifacts(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        source = "@export\ndef ping():\n    return 'pong'\n"
        forged_artifacts = {
            "format": "xian_contract_artifact_v1",
            "module_name": "con_forged_ir_probe",
            "vm_profile": "xian_vm_v2",
            "source": source,
            "vm_ir_json": "{}",
            "hashes": {},
        }

        output = run_call(
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_forged_ir_probe",
                "code": source,
                "deployment_artifacts": forged_artifacts,
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=False,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("deployment_artifacts", str(output.result))
        self.assertEqual(output.writes, {})
        driver.flush_full()

    def test_execute_native_submission_rejects_zk_contract_when_feature_disabled(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        set_driver_runtime_features(driver, {"zk": False})
        code = "@export\ndef check():\n    return zk.is_available()\n"
        output = run_call(
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_zk_probe",
                "code": code,
                "constructor_args": {},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=False,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("zk runtime feature is disabled", str(output.result))
        driver.flush_full()

    def test_execute_native_submission_change_owner_stages_metadata_write(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        driver.set_contract_from_source(
            "con_owned",
            "@export\ndef ping():\n    return 1\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )

        output = run_call(
            driver,
            sender="alice",
            contract_name="submission",
            function_name="change_owner",
            kwargs={"contract": "con_owned", "new_owner": "bob"},
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=False,
        )

        self.assertEqual(output.status_code, 0)
        self.assertEqual(output.writes["con_owned.__owner__"], "bob")
        self.assertTrue(
            any(event.get("event") == "ContractOwnerChanged" for event in output.events)
        )
        driver.flush_full()

    def test_execute_native_token_factory_deploys_child_contract(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        token_factory_source = (
            Path(__file__).resolve().parents[3]
            / "xian-configs"
            / "contracts"
            / "token_factory.s.py"
        ).read_text()
        driver.set_contract_from_source(
            "token_factory",
            token_factory_source,
            owner=None,
            developer="sys",
            deployer="sys",
            initiator="sys",
        )

        output = run_call(
            driver,
            sender="alice",
            contract_name="token_factory",
            function_name="create_token",
            kwargs={
                "token_contract": "con_demo_token",
                "token_name": "Demo Token",
                "token_symbol": "DEMO",
                "initial_supply": 42,
                "initial_holder": "bob",
                "operator_address": "carol",
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
            },
            meter=True,
            chi_budget=100_000,
            transaction_size_bytes=512,
        )

        self.assertEqual(output.status_code, 0)
        self.assertIn("con_demo_token.__source__", output.writes)
        self.assertIn("con_demo_token.__xian_ir_v1__", output.writes)
        self.assertEqual(output.writes["con_demo_token.balances:bob"], 42)
        self.assertEqual(
            output.writes["con_demo_token.metadata:token_symbol"],
            "DEMO",
        )
        self.assertEqual(output.writes["con_demo_token.operator"], "carol")
        self.assertTrue(any(event.get("event") == "ContractDeployed" for event in output.events))
        self.assertTrue(any(event.get("event") == "TokenCreated" for event in output.events))
        self.assertIn("token_factory", output.contract_costs)
        self.assertIn("submission", output.contract_costs)
        driver.flush_full()

    def test_execute_native_members_reregister_after_removal_resets_membership_dates(
        self,
    ):
        contracts_dir = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            for name, constructor_args in (
                ("currency", {"vk": "11" * 32}),
                ("dao", None),
                ("rewards", None),
                ("chi_cost", {"initial_rate": 20}),
            ):
                client.submit(
                    (contracts_dir / f"{name}.s.py").read_text(),
                    name=name,
                    constructor_args=constructor_args,
                    owner="members" if name in {"dao", "rewards"} else None,
                )

            for node in ("11" * 32, "22" * 32, "33" * 32, "44" * 32, "55" * 32):
                client.raw_driver.set(
                    client.raw_driver.make_key("currency", "balances", [node]),
                    1_000_000,
                )
            client.raw_driver.set(
                "rewards.S:value",
                [ContractingDecimal(text) for text in ("0.88", "0.01", "0.01", "0.1")],
            )

            client.submit(
                (contracts_dir / "validators.s.py").read_text(),
                name="members",
                constructor_args={
                    "genesis_registration_fee": 100_000,
                    "genesis_nodes": [
                        "11" * 32,
                        "22" * 32,
                        "33" * 32,
                        "44" * 32,
                        "55" * 32,
                    ],
                },
            )

            processor = TxProcessor(client=client)

            def tx(sender, contract, function, kwargs, nonce, height):
                return {
                    "payload": {
                        "sender": sender,
                        "contract": contract,
                        "function": function,
                        "kwargs": kwargs,
                        "nonce": nonce,
                        "chi_supplied": 200_000,
                        "chain_id": "test-chain",
                    },
                    "metadata": {"signature": "abc"},
                    "b_meta": {
                        "nanos": height * 1_000_000_000,
                        "hash": f"0x{height:064x}",
                        "height": height,
                        "chain_id": "test-chain",
                    },
                }

            remove_flow = (
                tx(
                    "11" * 32,
                    "members",
                    "propose_vote",
                    {"type_of_vote": "remove_member", "arg": "33" * 32},
                    1,
                    1,
                ),
                tx(
                    "22" * 32,
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    2,
                ),
                tx(
                    "44" * 32,
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    3,
                ),
                tx(
                    "55" * 32,
                    "members",
                    "vote",
                    {"proposal_id": 1, "vote": "yes"},
                    1,
                    4,
                ),
            )
            for call in remove_flow:
                result = _process(processor, call)
                self.assertIsNotNone(result["tx_result"])
                self.assertEqual(result["tx_result"]["status"], 0)

            approve_result = _process(
                processor,
                tx(
                    "33" * 32,
                    "currency",
                    "approve",
                    {"amount": 100_000, "to": "members"},
                    1,
                    5,
                ),
            )
            self.assertEqual(approve_result["tx_result"]["status"], 0)

            reregister_result = _process(
                processor,
                tx(
                    "33" * 32,
                    "members",
                    "register",
                    {
                        "requested_validator_power": 12,
                        "moniker": "node-3-return",
                        "network_endpoint": "localnet://node-3",
                    },
                    2,
                    6,
                ),
            )

            self.assertIsNotNone(reregister_result["tx_result"])
            self.assertEqual(reregister_result["tx_result"]["status"], 0)
            self.assertEqual(
                client.raw_driver.get(f"members.pending_registrations:{"33" * 32}"),
                True,
            )
            self.assertIsNone(client.raw_driver.get(f"members.joined_at:{"33" * 32}"))
            self.assertIsNone(client.raw_driver.get(f"members.left_at:{"33" * 32}"))
        finally:
            client.flush()

    def test_execute_native_permit_authorizer_consumes_nonce_and_emits_approval(self):
        contracts_dir = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            client.submit(
                (contracts_dir / "permit_authorizer.s.py").read_text(),
                name="permit_authorizer",
            )
            client.submit(
                (contracts_dir / "currency.s.py").read_text(),
                name="currency",
                constructor_args={"vk": "sys"},
            )
            client.get_contract_proxy("currency").balances["sys"] = 100_000

            owner = "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791"
            deadline = str(Datetime(2026, 4, 12, 12, 1))
            permit_msg = "\n".join(
                [
                    "xian-permit-v2",
                    "chain_id:test-chain",
                    "authorizer:permit_authorizer",
                    "token_contract:currency",
                    f"owner:{owner}",
                    "spender:some_spender",
                    "amount:100",
                    f"deadline:{deadline}",
                    "nonce:0",
                ]
            )
            permit_hash = hashlib.sha3_256(permit_msg.encode("utf-8")).hexdigest()
            outcome = run_paid(
                client.raw_driver,
                sender="sys",
                contract_name="permit_authorizer",
                function_name="permit",
                kwargs={
                    "token_contract": "currency",
                    "owner": owner,
                    "spender": "some_spender",
                    "value": 100,
                    "deadline": deadline,
                    "nonce": 0,
                    "signature": Ed25519Account(
                        "ed30796abc4ab47a97bfb37359f50a9c362c7b304a4b4ad1b3f5369ecb6f7fd8"
                    ).sign_msg(permit_msg),
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 23,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=15_000,
                transaction_size_bytes=512,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.writes[
                    "currency.approvals:"
                    "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791:"
                    "some_spender"
                ],
                100,
            )
            self.assertTrue(outcome.output.writes[f"permit_authorizer.permits:{permit_hash}"])
            self.assertEqual(
                outcome.output.writes[
                    "permit_authorizer.nonces:"
                    "ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791"
                ],
                1,
            )
            self.assertTrue(
                any(
                    event.get("contract") == "currency" and event.get("event") == "Approve"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_currency_transfer_accepts_exact_decimal(
        self,
    ):
        contracts_dir = Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            client.submit(
                (contracts_dir / "currency.s.py").read_text(),
                name="currency",
                constructor_args={"vk": "sys"},
            )
            client.raw_driver.delete("currency.__owner__")
            client.raw_driver.set(
                client.raw_driver.make_key("currency", "balances", ["worker0"]),
                ContractingDecimal("5000"),
            )
            client.raw_driver.set(
                client.raw_driver.make_key("currency", "balances", ["worker1"]),
                ContractingDecimal("5000"),
            )
            client.raw_driver.commit()
            outcome = run_paid(
                client.raw_driver,
                sender="worker0",
                contract_name="currency",
                function_name="transfer",
                kwargs={"amount": ContractingDecimal("1"), "to": "worker1"},
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 40,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=1_500,
                transaction_size_bytes=936,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.writes["currency.balances:worker0"],
                ContractingDecimal("4999")
                - (ContractingDecimal(outcome.chi_used) / ContractingDecimal(20)),
            )
            self.assertEqual(
                outcome.output.writes["currency.balances:worker1"],
                ContractingDecimal("5001"),
            )
            self.assertTrue(
                any(
                    event.get("contract") == "currency"
                    and event.get("event") == "Transfer"
                    and event.get("data", {}).get("amount") == ContractingDecimal("1")
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_submission_accepts_exact_decimal_constructor_argument(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir / "xian-stack" / "workloads" / "dex_mixed" / "token_fixture.py"
        ).read_text()
        contract_name = "con_tokena_probe"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 100_000)
            client.raw_driver.commit()
            outcome = run_paid(
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {
                        "owner": "sys",
                        "supply": ContractingDecimal("5000000"),
                        "name": "Workload Token A",
                        "symbol": "WTA",
                    },
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 54,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=150_000,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.balances:sys"],
                ContractingDecimal("5000000"),
            )
        finally:
            client.flush()

    def test_execute_native_submission_large_artifact_deployment_succeeds(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (root_dir / "xian-stack" / "workloads" / "dex_mixed" / "con_pairs.py").read_text()
        contract_name = "con_pairs_probe"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 250_000)
            client.raw_driver.commit()
            outcome = run_paid(
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {},
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 56,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=300_000,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertIn(f"{contract_name}.__xian_ir_v1__", outcome.output.writes)
        finally:
            client.flush()

    def test_execute_native_submission_persists_compact_shielded_artifacts(
        self,
    ):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-contracts"
            / "contracts"
            / "shielded-note-token"
            / "src"
            / "con_shielded_note_token.py"
        ).read_text()
        zk_registry_source = (
            root_dir / "xian-configs" / "contracts" / "zk_registry.s.py"
        ).read_text()
        contract_name = "con_shielded_note_probe"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            client.raw_driver.set("currency.balances:sys", 100_000_000)
            client.raw_driver.commit()
            client.submit(
                zk_registry_source,
                name="zk_registry",
                owner="governance",
            )
            outcome = run_paid(
                client.raw_driver,
                sender="sys",
                contract_name="submission",
                function_name="submit_contract",
                kwargs={
                    "name": contract_name,
                    "code": source,
                    "constructor_args": {
                        "token_name": "Local Private USD",
                        "token_symbol": "lpUSD",
                        "operator_address": "sys",
                        "root_window_size": 32,
                    },
                },
                environment={
                    "now": Datetime(2026, 4, 12, 12, 0),
                    "block_num": 58,
                    "block_hash": "abc123",
                    "chain_id": "test-chain",
                },
                chi_budget=25_000_000,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertIn(f"{contract_name}.__xian_ir_v1__", outcome.output.writes)
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.metadata:token_symbol"],
                "lpUSD",
            )
            self.assertTrue(
                any(event.get("event") == "ContractDeployed" for event in outcome.output.events)
            )
        finally:
            client.flush()

    def test_execute_native_shielded_configure_vk_updates_key_and_emits_event(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir
            / "xian-contracts"
            / "contracts"
            / "shielded-note-token"
            / "src"
            / "con_shielded_note_token.py"
        ).read_text()
        zk_registry_source = (
            root_dir / "xian-configs" / "contracts" / "zk_registry.s.py"
        ).read_text()
        contract_name = "con_shielded_note_probe"
        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            driver = client.raw_driver
            driver.set("currency.balances:sys", 100_000_000)
            driver.commit()
            client.submit(
                zk_registry_source,
                name="zk_registry",
                owner="sys",
            )
            client.submit(
                source,
                name=contract_name,
                constructor_args={
                    "token_name": "Local Private USD",
                    "token_symbol": "lpUSD",
                    "operator_address": "sys",
                    "root_window_size": 32,
                },
            )
            registry = client.get_contract_proxy("zk_registry")
            registry.register_vk(
                vk_id="demo-note",
                vk_hex="0x1234",
                circuit_name="demo",
                version="1",
                circuit_family="shielded_note_v4",
                statement_version="4",
                tree_depth=20,
                leaf_capacity=2**20,
                max_inputs=4,
                max_outputs=4,
                setup_mode="dev",
                setup_ceremony="test",
                artifact_hash="0x12",
                bundle_hash="0x34",
                warning="",
            )
            outcome = run_paid(
                driver,
                sender="sys",
                contract_name=contract_name,
                function_name="configure_vk",
                kwargs={
                    "action": "deposit",
                    "vk_id": "demo-note",
                },
                environment={
                    "now": Datetime(2026, 4, 13, 1, 0),
                    "block_num": 250,
                    "block_hash": "block-250",
                    "chain_id": "test-chain",
                },
                chi_budget=500_000,
                transaction_size_bytes=0,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(outcome.output.result, "demo-note")
            self.assertEqual(
                outcome.output.writes[f"{contract_name}.vk_ids:deposit"],
                "demo-note",
            )
            self.assertTrue(
                any(
                    event.get("event") == "VerifyingKeyConfigured"
                    and event.get("data_indexed", {}).get("action") == "deposit"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_native_hash_prefix_scan_reads_and_writes(self):
        root_dir = Path(__file__).resolve().parents[3]
        source = (
            root_dir / "xian-stack" / "workloads" / "parallel_probe" / "con_parallel_probe.py"
        ).read_text()

        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        try:
            driver = client.raw_driver
            driver.set("currency.balances:sys", 100_000_000)
            driver.commit()
            client.submit(source, name="con_parallel_probe", owner="sys")
            driver.set("con_parallel_probe.values:g:seed", 13)
            driver.commit()
            outcome = run_paid(
                driver,
                sender="sys",
                contract_name="con_parallel_probe",
                function_name="snapshot_sum",
                kwargs={"group": "g", "tag": "obs-1"},
                environment={
                    "now": Datetime(2026, 4, 13, 1, 0),
                    "block_num": 251,
                    "block_hash": "block-251",
                    "chain_id": "test-chain",
                },
                chi_budget=2_000,
                transaction_size_bytes=len(source.encode("utf-8")),
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(outcome.output.result, 13)
            self.assertEqual(
                outcome.output.writes["con_parallel_probe.observations:obs-1"],
                13,
            )
        finally:
            client.flush()

    def test_execute_native_dex_add_liquidity_has_exact_reserves(self):
        root_dir = Path(__file__).resolve().parents[3]
        workload_dir = root_dir / "xian-stack" / "workloads" / "dex_mixed"
        token_source = (workload_dir / "token_fixture.py").read_text()
        pairs_source = (workload_dir / "con_pairs.py").read_text()
        pairs_name = "con_pairs_probe"
        dex_name = "con_dex_probe"
        dex_source = (
            (workload_dir / "con_dex.py")
            .read_text()
            .replace(
                'DEX_PAIRS = "con_pairs"',
                f'DEX_PAIRS = "{pairs_name}"',
                1,
            )
        )

        client = ContractingClient(vm_profile="xian_vm_v2", environment={"chain_id": "test-chain"})
        self.addCleanup(client.close)
        client.flush()
        driver = client.raw_driver

        def deploy_contract(name, source, constructor_args):
            client.submit(source, name=name, signer="sys", constructor_args=constructor_args)
            driver.commit()

        try:
            driver.set("currency.balances:sys", 1_000_000)
            driver.commit()

            deploy_contract(
                "con_tokena_probe",
                token_source,
                {
                    "owner": "sys",
                    "supply": ContractingDecimal("5000000"),
                    "name": "Token A",
                    "symbol": "TA",
                },
            )
            deploy_contract(
                "con_tokenb_probe",
                token_source,
                {
                    "owner": "sys",
                    "supply": ContractingDecimal("5000000"),
                    "name": "Token B",
                    "symbol": "TB",
                },
            )
            deploy_contract(pairs_name, pairs_source, {})
            deploy_contract(dex_name, dex_source, {})

            execution_environment = {
                "now": Datetime(2026, 4, 12, 12, 5),
                "block_num": 77,
                "block_hash": "block-77",
                "chain_id": "test-chain",
            }
            for token_name in ("con_tokena_probe", "con_tokenb_probe"):
                client.get_contract_proxy(token_name).approve(
                    amount=500_000, to=dex_name, signer="sys"
                )
                driver.commit()

            outcome = run_paid(
                driver,
                sender="sys",
                contract_name=dex_name,
                function_name="addLiquidity",
                kwargs={
                    "tokenA": "con_tokena_probe",
                    "tokenB": "con_tokenb_probe",
                    "amountADesired": 250_000,
                    "amountBDesired": 250_000,
                    "amountAMin": 240_000,
                    "amountBMin": 240_000,
                    "to": "sys",
                    "deadline": Datetime(2026, 4, 12, 12, 10),
                },
                environment=execution_environment,
                chi_budget=60_000,
                transaction_size_bytes=0,
            )

            self.assertEqual(outcome.output.status_code, 0)
            self.assertEqual(
                outcome.output.result,
                (
                    ContractingDecimal("250000"),
                    ContractingDecimal("250000"),
                    ContractingDecimal("249999.99999999"),
                ),
            )
            self.assertEqual(
                outcome.output.writes["con_pairs_probe.pairs:1:reserve0"],
                ContractingDecimal("250000"),
            )
            self.assertEqual(
                outcome.output.writes["con_pairs_probe.pairs:1:reserve1"],
                ContractingDecimal("250000"),
            )
            self.assertTrue(
                any(
                    event.get("contract") == "con_pairs_probe" and event.get("event") == "Mint"
                    for event in outcome.output.events
                )
            )
        finally:
            client.flush()

    def test_execute_vm_contract_rejects_source_only_contracts(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        driver.set_contract_from_source(
            "con_source_only",
            "@export\ndef ping():\n    return 'pong'\n",
            owner="alice",
            developer="alice",
            deployer="alice",
            initiator="alice",
        )
        driver.delete(driver.make_key("con_source_only", "__xian_ir_v1__"))

        output = run_call(
            driver,
            sender="alice",
            contract_name="con_source_only",
            function_name="ping",
            kwargs={},
            environment={},
            meter=False,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("requires persisted __xian_ir_v1__", str(output.result))

    def test_execute_native_submission_requires_deterministic_now(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        source = "@export\ndef ping():\n    return 'pong'\n"

        output = run_call(
            driver,
            sender="sys",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_deterministic_probe",
                "code": source,
            },
            environment={},
            meter=False,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("deterministic now context", str(output.result))

    def test_execute_native_submission_enforces_constructor_write_capacity_limit(
        self,
    ):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        source = (
            "blob = Variable()\n\n"
            "@construct\n"
            "def seed(payload: str):\n"
            "    blob.set(payload)\n\n"
            "@export\n"
            "def blob_size():\n"
            "    return len(blob.get())\n"
        )
        output = run_call(
            driver,
            sender="alice",
            contract_name="submission",
            function_name="submit_contract",
            kwargs={
                "name": "con_write_limit_probe",
                "code": source,
                "constructor_args": {"payload": "a" * 140_000},
            },
            environment={
                "now": Datetime(2026, 4, 12, 12, 0),
                "block_num": 7,
                "block_hash": "abc123",
                "chain_id": "xian-local",
                "__xian_execution_mode__": "xian_vm_v2",
            },
            meter=True,
            chi_budget=180_000,
            transaction_size_bytes=150_000,
        )

        self.assertEqual(output.status_code, 1)
        self.assertIn("maximum write capacity", str(output.result))
        self.assertEqual(output.writes, {})
        self.assertEqual(output.events, [])

    def test_dynamic_importlib_call_preserves_context_and_writes(self):
        driver = Driver(vm_profile="xian_vm_v2")
        self.addCleanup(driver.close)
        driver.flush_full()
        client = ContractingClient(vm_profile="xian_vm_v2", driver=driver)
        self.addCleanup(client.close)
        driver.set(driver.make_key("currency", "balances", ["alice"]), 1_000_000)

        environment = {
            "now": Datetime(2026, 4, 12, 12, 0),
            "block_num": 7,
            "block_hash": "abc123",
            "chain_id": "xian-local",
            "__xian_execution_mode__": "xian_vm_v2",
        }
        leaf_name = "con_dynamic_leaf"
        leaf_source = (
            "touch_total = Variable()\n\n"
            "@construct\n"
            "def seed():\n"
            "    touch_total.set(0)\n\n"
            "@export\n"
            "def touch(account: str, amount: int):\n"
            "    touch_total.set((touch_total.get() or 0) + amount)\n"
            "    return touch_total.get()\n"
        )
        router_name = "con_dynamic_router"
        router_source = (
            "@export\n"
            "def dynamic_touch(target_contract: str, function_name: str, account: str, amount: int):\n"
            "    return {\n"
            "        'router_ctx': {\n"
            "            'this': ctx.this,\n"
            "            'caller': ctx.caller,\n"
            "            'signer': ctx.signer,\n"
            "            'entry': f'{ctx.entry[0]}.{ctx.entry[1]}',\n"
            "        },\n"
            "        'result': importlib.call(\n"
            "            target_contract,\n"
            "            function_name,\n"
            "            {'account': account, 'amount': amount},\n"
            "        ),\n"
            "    }\n"
        )

        for name, source in (
            (leaf_name, leaf_source),
            (router_name, router_source),
        ):
            client.submit(source, name=name, signer="alice")
            driver.commit()

        call_kwargs = {
            "target_contract": leaf_name,
            "function_name": "touch",
            "account": "alice",
            "amount": 3,
        }
        before_state = driver.snapshot_state()
        native_output = run_call(
            driver,
            sender="alice",
            contract_name=router_name,
            function_name="dynamic_touch",
            kwargs=call_kwargs,
            environment=environment,
            meter=False,
            chi_budget=0,
            transaction_size_bytes=0,
        )
        driver.restore_state(before_state)
        self.assertEqual(native_output.status_code, 0)
        self.assertEqual(
            native_output.result,
            {
                "router_ctx": {
                    "this": router_name,
                    "caller": "alice",
                    "signer": "alice",
                    "entry": f"{router_name}.dynamic_touch",
                },
                "result": 3,
            },
        )
        self.assertEqual(
            native_output.writes,
            {f"{leaf_name}.touch_total": 3},
        )

