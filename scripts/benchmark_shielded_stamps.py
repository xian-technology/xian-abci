# ruff: noqa: E402

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
XIAN_ZK_PYTHON = (
    WORKSPACE_ROOT / "xian-contracting" / "packages" / "xian-zk" / "python"
)
if str(XIAN_ZK_PYTHON) not in sys.path:
    sys.path.insert(0, str(XIAN_ZK_PYTHON))

from contracting.client import ContractingClient
from xian_zk import (
    ShieldedDepositRequest,
    ShieldedKeyBundle,
    ShieldedNote,
    ShieldedNoteProver,
    ShieldedOutput,
    ShieldedRelayTransferProver,
    ShieldedRelayTransferWallet,
    ShieldedTransferRequest,
    ShieldedWallet,
    ShieldedWithdrawRequest,
    asset_id_for_contract,
    output_payload_hashes,
    scan_notes,
    shielded_relay_registry_manifest,
    tree_state,
    zero_root,
)

from xian.processor import TxProcessor

BASELINE = {
    "deposit_2_outputs": 87622,
    "transfer_2in_2out": 87896,
    "withdraw_1in_1out": 45081,
    "withdraw_exact": 2107,
    "relay_transfer": 113839,
}


def field(value: int) -> str:
    return "0x" + format(value, "064x")


def create_block_meta(height: int) -> dict[str, object]:
    nanos = int(time.time() * 1e9)
    return {
        "nanos": nanos,
        "height": height,
        "chain_id": "test-chain",
        "hash": f"block-{height}",
    }


def _load_contract_paths() -> tuple[Path, Path]:
    test_file = (
        WORKSPACE_ROOT
        / "xian-contracts"
        / "contracts"
        / "shielded-note-token"
        / "tests"
        / "test_shielded_note_token.py"
    )
    spec = importlib.util.spec_from_file_location(
        "shielded_note_token_test", test_file
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load shielded note token test fixture")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CONTRACT_PATH, module.ZK_REGISTRY_PATH


CONTRACT_PATH, ZK_REGISTRY_PATH = _load_contract_paths()


def setup_contract(
    client: ContractingClient,
    note_prover: ShieldedNoteProver,
    relay_manifest: dict[str, object],
) -> object:
    with ZK_REGISTRY_PATH.open() as handle:
        client.raw_driver.set_contract_from_source(
            name="zk_registry",
            source=handle.read(),
            lint=False,
        )
    client.raw_driver.commit()
    registry = client.get_contract("zk_registry")
    registry.seed(owner="sys", signer="sys")

    for action in ("deposit", "transfer", "withdraw"):
        bundle = note_prover.bundle[action]
        registry.register_vk(
            vk_id=bundle["vk_id"],
            vk_hex=bundle["vk_hex"],
            circuit_name=bundle["circuit_name"],
            version=bundle["version"],
            artifact_contract_name="con_shielded_note_token",
            circuit_family="shielded_note_v3",
            statement_version=bundle["version"],
            tree_depth=note_prover.bundle["tree_depth"],
            leaf_capacity=note_prover.bundle["leaf_capacity"],
            max_inputs=note_prover.bundle["max_inputs"],
            max_outputs=note_prover.bundle["max_outputs"],
            setup_mode="insecure-dev",
            signer="sys",
        )

    for entry in relay_manifest["registry_entries"]:
        args = dict(entry)
        args.pop("action", None)
        registry.register_vk(**args, signer="sys")

    with CONTRACT_PATH.open() as handle:
        client.submit(
            handle.read(),
            name="con_shielded_note_token",
            constructor_args={"root_window_size": 3},
        )
    token = client.get_contract("con_shielded_note_token")
    token.configure_vk(
        action="deposit",
        vk_id=note_prover.bundle["deposit"]["vk_id"],
        signer="sys",
    )
    token.configure_vk(
        action="transfer",
        vk_id=note_prover.bundle["transfer"]["vk_id"],
        signer="sys",
    )
    token.configure_vk(
        action="withdraw",
        vk_id=note_prover.bundle["withdraw"]["vk_id"],
        signer="sys",
    )
    token.configure_vk(
        action="relay_transfer",
        vk_id=relay_manifest["configure_actions"][0]["vk_id"],
        signer="sys",
    )
    return token


def process(
    processor: TxProcessor,
    *,
    function: str,
    sender: str,
    kwargs: dict[str, object],
    height: int,
    stamps: int = 25_000_000,
) -> int:
    tx = {
        "payload": {
            "contract": "con_shielded_note_token",
            "function": function,
            "sender": sender,
            "kwargs": kwargs,
            "stamps_supplied": stamps,
        },
        "metadata": {"signature": "benchmark"},
        "b_meta": create_block_meta(height),
    }
    result = processor.process_tx(enabled_fees=True, tx=tx)
    tx_result = result["tx_result"]
    if tx_result["status"] != 0:
        raise RuntimeError(f"{function} failed: {tx_result['result']}")
    return int(tx_result["stamps_used"])


def benchmark() -> dict[str, int]:
    with tempfile.TemporaryDirectory() as storage_home:
        client = ContractingClient(storage_home=Path(storage_home))
        client.flush()
        processor = TxProcessor(client=client)
        note_prover = ShieldedNoteProver.build_insecure_dev_bundle()
        relay_prover = ShieldedRelayTransferProver.build_insecure_dev_bundle()
        relay_manifest = shielded_relay_registry_manifest(
            relay_prover,
            artifact_contract_name="con_shielded_note_token",
        )

        results: dict[str, int] = {}

        setup_contract(client, note_prover, relay_manifest)
        token = client.get_contract("con_shielded_note_token")
        alice = "alice"
        bob = "bob"
        relayer = "relayer-1"
        client.raw_driver.set("currency.balances:alice", 1_000_000_000)
        client.raw_driver.set("currency.balances:relayer-1", 1_000_000_000)
        token.mint_public(amount=100, to=alice, signer="sys")

        asset_id = asset_id_for_contract("con_shielded_note_token")
        alice_wallet = ShieldedWallet.from_parts(
            asset_id=asset_id,
            owner_secret=field(101),
            viewing_private_key="11" * 32,
        )
        bob_keys = ShieldedKeyBundle.from_parts(
            owner_secret=field(202),
            viewing_private_key="22" * 32,
        )

        alice_note_1 = ShieldedNote(
            owner_secret=alice_wallet.owner_secret,
            amount=40,
            rho=field(1001),
            blind=field(2001),
        )
        alice_note_2 = ShieldedNote(
            owner_secret=alice_wallet.owner_secret,
            amount=30,
            rho=field(1002),
            blind=field(2002),
        )
        deposit_outputs = [alice_note_1.to_output(), alice_note_2.to_output()]
        deposit_payloads = [
            output.encrypt_for(
                asset_id=asset_id,
                viewing_public_key=alice_wallet.viewing_public_key,
            )
            for output in deposit_outputs
        ]
        deposit = note_prover.prove_deposit(
            ShieldedDepositRequest(
                asset_id=asset_id,
                old_root=zero_root(),
                append_state=tree_state([]),
                amount=70,
                outputs=deposit_outputs,
                output_payload_hashes=output_payload_hashes(deposit_payloads),
            )
        )
        results["deposit_2_outputs"] = process(
            processor,
            function="deposit_shielded",
            sender=alice,
            kwargs={
                "amount": 70,
                "old_root": deposit.old_root,
                "output_commitments": deposit.output_commitments,
                "proof_hex": deposit.proof_hex,
                "output_payloads": deposit_payloads,
            },
            height=1,
        )

        commitments = deposit.output_commitments
        inputs = scan_notes(
            asset_id=asset_id,
            commitments=commitments,
            notes=[alice_note_1, alice_note_2],
        )
        bob_note = ShieldedNote(
            owner_secret=bob_keys.owner_secret,
            amount=25,
            rho=field(1003),
            blind=field(2003),
        )
        alice_change = ShieldedNote(
            owner_secret=alice_wallet.owner_secret,
            amount=45,
            rho=field(1004),
            blind=field(2004),
        )
        transfer_outputs = [
            ShieldedOutput.for_recipient(
                bob_keys.recipient,
                amount=25,
                rho=bob_note.rho,
                blind=bob_note.blind,
            ),
            alice_change.to_output(),
        ]
        transfer_payloads = [
            transfer_outputs[0].encrypt_for(
                asset_id=asset_id,
                viewing_public_key=bob_keys.viewing_public_key,
            ),
            transfer_outputs[1].encrypt_for(
                asset_id=asset_id,
                viewing_public_key=alice_wallet.viewing_public_key,
            ),
        ]
        transfer = note_prover.prove_transfer(
            ShieldedTransferRequest(
                asset_id=asset_id,
                old_root=deposit.expected_new_root,
                append_state=tree_state(commitments),
                inputs=[note.to_input() for note in inputs],
                outputs=transfer_outputs,
                output_payload_hashes=output_payload_hashes(transfer_payloads),
            )
        )
        results["transfer_2in_2out"] = process(
            processor,
            function="transfer_shielded",
            sender=alice,
            kwargs={
                "old_root": transfer.old_root,
                "input_nullifiers": transfer.input_nullifiers,
                "output_commitments": transfer.output_commitments,
                "proof_hex": transfer.proof_hex,
                "output_payloads": transfer_payloads,
            },
            height=2,
        )

        commitments = deposit.output_commitments + transfer.output_commitments
        withdraw_inputs = scan_notes(
            asset_id=asset_id,
            commitments=commitments,
            notes=[alice_change],
        )
        alice_change_2 = ShieldedNote(
            owner_secret=alice_wallet.owner_secret,
            amount=25,
            rho=field(1005),
            blind=field(2005),
        )
        withdraw_payloads = [
            alice_change_2.to_output().encrypt_for(
                asset_id=asset_id,
                viewing_public_key=alice_wallet.viewing_public_key,
            )
        ]
        withdraw = note_prover.prove_withdraw(
            ShieldedWithdrawRequest(
                asset_id=asset_id,
                old_root=transfer.expected_new_root,
                append_state=tree_state(commitments),
                amount=20,
                recipient=bob,
                inputs=[withdraw_inputs[0].to_input()],
                outputs=[alice_change_2.to_output()],
                output_payload_hashes=output_payload_hashes(withdraw_payloads),
            )
        )
        results["withdraw_1in_1out"] = process(
            processor,
            function="withdraw_shielded",
            sender=alice,
            kwargs={
                "amount": 20,
                "to": bob,
                "old_root": withdraw.old_root,
                "input_nullifiers": withdraw.input_nullifiers,
                "output_commitments": withdraw.output_commitments,
                "proof_hex": withdraw.proof_hex,
                "output_payloads": withdraw_payloads,
            },
            height=3,
        )

        commitments = commitments + withdraw.output_commitments
        exact_inputs = scan_notes(
            asset_id=asset_id,
            commitments=commitments,
            notes=[alice_change_2],
        )
        exact_withdraw = note_prover.prove_withdraw(
            ShieldedWithdrawRequest(
                asset_id=asset_id,
                old_root=withdraw.expected_new_root,
                append_state=tree_state(commitments),
                amount=25,
                recipient=bob,
                inputs=[exact_inputs[0].to_input()],
                outputs=[],
                output_payload_hashes=[],
            )
        )
        results["withdraw_exact"] = process(
            processor,
            function="withdraw_shielded",
            sender=alice,
            kwargs={
                "amount": 25,
                "to": bob,
                "old_root": exact_withdraw.old_root,
                "input_nullifiers": exact_withdraw.input_nullifiers,
                "output_commitments": exact_withdraw.output_commitments,
                "proof_hex": exact_withdraw.proof_hex,
                "output_payloads": [],
            },
            height=4,
        )

        client.flush()
        setup_contract(client, note_prover, relay_manifest)
        token = client.get_contract("con_shielded_note_token")
        client.raw_driver.set("currency.balances:alice", 1_000_000_000)
        client.raw_driver.set("currency.balances:relayer-1", 1_000_000_000)
        token.mint_public(amount=100, to=alice, signer="sys")
        asset_id = asset_id_for_contract("con_shielded_note_token")
        alice_wallet = ShieldedWallet.from_parts(
            asset_id=asset_id,
            owner_secret=field(301),
            viewing_private_key="31" * 32,
        )
        relay_wallet = ShieldedRelayTransferWallet.from_json(
            alice_wallet.to_json()
        )
        bob_keys = ShieldedKeyBundle.from_parts(
            owner_secret=field(302),
            viewing_private_key="32" * 32,
        )
        relay_deposit_note = ShieldedNote(
            owner_secret=alice_wallet.owner_secret,
            amount=30,
            rho=field(3001),
            blind=field(4001),
        )
        relay_deposit_payload = relay_deposit_note.to_output().encrypt_for(
            asset_id=asset_id,
            viewing_public_key=alice_wallet.viewing_public_key,
        )
        relay_deposit = note_prover.prove_deposit(
            ShieldedDepositRequest(
                asset_id=asset_id,
                old_root=zero_root(),
                append_state=tree_state([]),
                amount=30,
                outputs=[relay_deposit_note.to_output()],
                output_payload_hashes=output_payload_hashes(
                    [relay_deposit_payload]
                ),
            )
        )
        process(
            processor,
            function="deposit_shielded",
            sender=alice,
            kwargs={
                "amount": 30,
                "old_root": relay_deposit.old_root,
                "output_commitments": relay_deposit.output_commitments,
                "proof_hex": relay_deposit.proof_hex,
                "output_payloads": [relay_deposit_payload],
            },
            height=10,
        )
        relay_wallet.sync_transactions(
            [
                {
                    "tx_hash": "relay-deposit",
                    "block_height": 10,
                    "tx_index": 0,
                    "success": True,
                    "payload": {
                        "sender": alice,
                        "nonce": 1,
                        "contract": "con_shielded_note_token",
                        "function": "deposit_shielded",
                        "kwargs": {
                            "amount": 30,
                            "old_root": relay_deposit.old_root,
                            "output_commitments": relay_deposit.output_commitments,
                            "proof_hex": relay_deposit.proof_hex,
                            "output_payloads": [relay_deposit_payload],
                        },
                    },
                }
            ]
        )
        relay_plan = relay_wallet.build_relay_transfer(
            recipient=bob_keys.recipient,
            amount=14,
            relayer=relayer,
            chain_id="test-chain",
            fee=2,
            recipient_memo="relay",
        )
        relay_transfer = relay_prover.prove_relay_transfer(relay_plan.request)
        results["relay_transfer"] = process(
            processor,
            function="relay_transfer_shielded",
            sender=relayer,
            kwargs={
                "old_root": relay_transfer.old_root,
                "input_nullifiers": relay_transfer.input_nullifiers,
                "output_commitments": relay_transfer.output_commitments,
                "proof_hex": relay_transfer.proof_hex,
                "relayer_fee": relay_transfer.relayer_fee,
                "expires_at": relay_plan.request.expires_at,
                "output_payloads": relay_plan.output_payloads,
            },
            height=11,
        )

        return results


def compare(current: dict[str, int]) -> dict[str, dict[str, float | int]]:
    comparison: dict[str, dict[str, float | int]] = {}
    for key, current_value in current.items():
        baseline_value = BASELINE[key]
        comparison[key] = {
            "baseline": baseline_value,
            "current": current_value,
            "delta": current_value - baseline_value,
            "ratio": round(current_value / baseline_value, 4),
        }
    return comparison


if __name__ == "__main__":
    current = benchmark()
    print(
        json.dumps(
            {"current": current, "comparison": compare(current)}, indent=2
        )
    )
