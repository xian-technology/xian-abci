"""Execution revision 14's explicit rules for inputs the Python path halted on
or decided by its own quirks (single-engine plan, increment 7). The engine
parity corpora pin the common ones (odd sender balances, negative balances,
odd reward configurations); these cases have no corpus entry. Every
transaction gets a receipt; none halts the block."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.decimal import ContractingDecimal

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.encoding import encode_abci_json
from xian.utils.tx import transaction_bytes

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
ALICE = "a" * 64
PING = """counter = Variable()

@export
def ping():
    counter.set((counter.get() or 0) + 1)
    return counter.get()
"""
POISONED_CURRENCY = """balances = Hash(default_value=0)

@export
def poison():
    balances[ctx.caller] = "not a number"
"""


def _chain(home, *, currency=None):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
    driver = client.raw_driver
    driver.set_contract_from_source(
        "currency", currency or (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_ping", PING, developer="dev")
    driver.set(f"currency.balances:{ALICE}", ContractingDecimal("10") ** 18)
    driver.set("chi_cost.S:value", 20)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["validator"])
    driver.set("rewards.S:value", [ContractingDecimal("0.7"), 0, 0, ContractingDecimal("0.3")])
    driver.commit()
    return client, TxProcessor(client=client)


def _run(chain, contract, function, *, chi=5_000, kwargs=None):
    client, processor = chain
    tx = {
        "payload": {
            "chain_id": "xian-local",
            "chi_supplied": chi,
            "contract": contract,
            "function": function,
            "kwargs": kwargs or {},
            "nonce": 0,
            "sender": ALICE,
        },
        "metadata": {"signature": "f" * 128},
        "b_meta": {
            "nanos": 1_790_000_000_000_000_000,
            "height": 1,
            "hash": "0" * 64,
            "chain_id": "xian-local",
        },
    }
    (response,) = processor.process_block(
        [tx],
        [transaction_bytes(tx)],
        fee_policy=TxFeePolicy.paid_metered(),
    )
    client.raw_driver.commit()
    return json.loads(encode_abci_json(response["tx_result"]))


@pytest.fixture
def chain(tmp_path):
    chain = _chain(tmp_path)
    yield chain
    chain[0].raw_driver.close()


def test_an_owner_that_is_not_a_string_is_no_caller(chain):
    chain[0].raw_driver.set("con_ping.__owner__", 5)
    receipt = _run(chain, "con_ping", "ping")
    assert (receipt["status"], receipt["chi_used"]) == (1, 5)
    assert receipt["result"] == "Exception('Caller is not the owner!')"


def test_a_persisted_ir_that_is_no_valid_module_has_one_failure_text(chain):
    chain[0].raw_driver.set("con_odd.__xian_ir_v1__", '{"ir_version": "xian_ir_v1"}')
    receipt = _run(chain, "con_odd", "ping")
    assert receipt["status"] == 1
    assert receipt["result"] == "ValueError(\"contract 'con_odd' has invalid persisted VM IR\")"


def test_a_budget_beyond_the_raw_chi_range_fails_in_the_host_runtime(chain):
    receipt = _run(chain, "con_ping", "ping", chi=2**63)
    assert (receipt["status"], receipt["chi_used"]) == (1, 5)
    assert receipt["result"] == "RuntimeError('Transaction failed in the host runtime.')"


def test_a_reward_recipient_key_lmdb_cannot_store_voids_the_payout(chain):
    chain[0].raw_driver.set("con_ping.__developer__", "d" * 600)
    receipt = _run(chain, "con_ping", "ping")
    assert receipt["status"] == 0
    assert receipt["rewards"] is None and receipt["reward_records"] is None
    # The fee is charged (burned): only the sender's balance is written.
    assert [write["key"] for write in receipt["state"] if write["key"].startswith("currency.")] == [
        f"currency.balances:{ALICE}"
    ]


def test_a_balance_execution_leaves_as_no_number_fails_the_transaction(tmp_path):
    chain = _chain(tmp_path, currency=POISONED_CURRENCY)
    try:
        receipt = _run(chain, "currency", "poison")
        assert receipt["status"] == 1
        assert receipt["result"] == "AssertionError('Sender balance is not a number.')"
        # Its effects are dropped; the fee is charged from the balance before it.
        (write,) = [w for w in receipt["state"] if w["key"] == f"currency.balances:{ALICE}"]
        assert write["value"] != "not a number"
        assert receipt["chi_used"] > 5
    finally:
        chain[0].raw_driver.close()


def test_a_submission_name_that_is_not_text_is_refused_by_the_deployment(chain):
    receipt = _run(
        chain,
        "submission",
        "submit_contract",
        chi=100_000,
        kwargs={"name": 5, "code": PING},
    )
    assert receipt["status"] == 1
    assert "Contract name must be a non-empty string" in receipt["result"]


def test_effects_that_write_a_protected_key_fail_with_their_chi(chain, monkeypatch):
    # No V2 contract can name the execution policy key; protect a key the
    # contract writes to reach the check.
    monkeypatch.setattr("xian.processor.EXECUTION_POLICY_KEY", "con_ping.counter")
    receipt = _run(chain, "con_ping", "ping")
    assert receipt["status"] == 1
    assert receipt["result"] == (
        "ValueError('Contract writes or events cannot be encoded for consensus.')"
    )
    assert receipt["chi_used"] > 5
    assert not any(write["key"].startswith("con_ping.") for write in receipt["state"])
