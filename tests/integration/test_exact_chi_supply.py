"""A transaction that supplies exactly its simulated chi executes as simulated.

Clients supply the simulated ``chi_used`` with no headroom. That only works
if, for the same state, the simulator (no fee reservation, an estimated
transaction size) reports the chi that block execution (fee reservation, the
real signed size, the Rust block executor) charges. Each case simulates a
call, executes it in a block with ``chi_supplied`` equal to the estimate, and
compares status and ``chi_used``.

Execution caps the charge at the supplied chi, so the same call is also run
with a generous budget: its cost may differ from the estimate only by the
signed-size bytes the simulator cannot know (it sizes the transaction with
nonce 0 and the simulation cap as ``chi_supplied``), never by a whole chi of
work, so the exact receipt does not hide a higher real cost.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from contracting.local import ContractingClient
from xian_runtime_types.decimal import ContractingDecimal

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.simulator import TransactionSimulator

pytest.importorskip("xian_vm_core")

ROOT = Path(__file__).resolve().parents[3]
CHAIN_ID = "xian-local-exact"
SIMULATION_CAP = 200_000
ALICE, BOB, CAROL = "a" * 64, "b" * 64, "c" * 64
POOL = """import currency
reserves = Hash(default_value=0)
tokens = Hash(default_value=0)
Swap = LogEvent("Swap", {"trader": indexed(str), "amount_in": (int, decimal), "amount_out": (int, decimal)})

@export
def swap(amount_in: decimal):
    reserve_in = reserves["xian"]
    reserve_out = reserves["token"]
    amount_out = amount_in * reserve_out / (reserve_in + amount_in)
    currency.transfer_from(amount=amount_in, to=ctx.this, main_account=ctx.caller)
    reserves["xian"] = reserve_in + amount_in
    reserves["token"] = reserve_out - amount_out
    tokens[ctx.caller] += amount_out
    Swap({"trader": ctx.caller, "amount_in": amount_in, "amount_out": amount_out})
    return amount_out

@export
def fail_after_swap(amount_in: decimal):
    swap(amount_in)
    assert False, "deliberate failure"
"""
DEPLOYED = """value = Variable()

@export
def set_value(new_value: int):
    value.set(new_value)
"""


def _setup(home):
    client = ContractingClient(vm_profile="xian_vm_v2", storage_home=home)
    driver = client.raw_driver
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_pool", POOL, developer="dev")
    balances = {
        ALICE: "1234567.891",
        BOB: "98765432.1",
        CAROL: "5000050.5",
    }
    for account, balance in balances.items():
        driver.set(f"currency.balances:{account}", ContractingDecimal(balance))
        driver.set(f"currency.approvals:{account}:con_pool", 10**9)
    driver.set("con_pool.reserves:xian", ContractingDecimal("5000"))
    driver.set("con_pool.reserves:token", ContractingDecimal("20000"))
    driver.set("chi_cost.S:value", 20)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["validator"])
    driver.set("rewards.S:value", [ContractingDecimal("0.7"), 0, 0, ContractingDecimal("0.3")])
    driver.commit()
    simulator = TransactionSimulator(client=client, chain_id=CHAIN_ID)
    return client, TxProcessor(client=client), RewardsHandler(client), simulator


def _block_meta(height):
    return {
        "nanos": 1_790_000_000_000_000_000 + height * 5_000_000_000,
        "height": height,
        "hash": f"{height:064x}",
        "chain_id": CHAIN_ID,
    }


def _transaction(call, *, nonce, chi_supplied, block_meta):
    sender, contract, function, kwargs = call
    return {
        "payload": {
            "chain_id": CHAIN_ID,
            "chi_supplied": chi_supplied,
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "nonce": nonce,
            "sender": sender,
        },
        "metadata": {"signature": "f" * 128},
        "b_meta": block_meta,
    }


def _execute(processor, rewards, transaction):
    responses = processor.process_block(
        [transaction], fee_policy=TxFeePolicy.paid_metered(), rewards_handler=rewards
    )
    return responses[0]["tx_result"]


def _simulate_then_execute(chain, call, *, nonce, height):
    """(simulated, executed with a generous budget, executed with the estimate)."""
    client, processor, rewards, simulator = chain
    block_meta = _block_meta(height)
    sender, contract, function, kwargs = call
    simulated = simulator.simulate(
        {"sender": sender, "contract": contract, "function": function, "kwargs": kwargs},
        block_meta=block_meta,
        max_chi=SIMULATION_CAP,
    )
    estimate = simulated["chi_used"]
    driver = client.raw_driver
    before = driver.snapshot_state()
    generous = _execute(
        processor,
        rewards,
        _transaction(call, nonce=nonce, chi_supplied=SIMULATION_CAP, block_meta=block_meta),
    )
    driver.restore_state(before)
    exact = _execute(
        processor,
        rewards,
        _transaction(call, nonce=nonce, chi_supplied=estimate, block_meta=block_meta),
    )
    driver.commit()
    return simulated, generous, exact


def _assert_exact_supply_executes_as_simulated(simulated, generous, exact):
    assert simulated["chi_used"] > 0
    assert generous["status"] == simulated["status"]
    assert abs(generous["chi_used"] - simulated["chi_used"]) <= 1
    assert (exact["status"], exact["chi_used"]) == (simulated["status"], simulated["chi_used"])


def _swap(sender, amount):
    return (sender, "con_pool", "swap", {"amount_in": {"__fixed__": amount}})


SCENARIOS = [
    pytest.param(
        (ALICE, "currency", "transfer", {"amount": {"__fixed__": "12.5"}, "to": "d" * 64}),
        0,
        id="transfer-to-new-recipient",
    ),
    pytest.param(
        (BOB, "currency", "transfer", {"amount": 1, "to": ALICE}),
        7,
        id="transfer-to-existing-recipient",
    ),
    pytest.param(_swap(CAROL, "33.3"), 1_234_567, id="swap-with-event"),
    # Its raw cost sits just above a whole chi: the simulator must not size
    # the envelope larger than the signed transaction (nonce 0, the estimate
    # as chi_supplied), or execution charges one chi less than estimated.
    pytest.param(
        (ALICE, "currency", "transfer", {"amount": 1, "to": "d" * 50}),
        0,
        id="transfer-at-a-chi-boundary",
    ),
    pytest.param(
        (ALICE, "submission", "submit_contract", {"name": "con_deployed", "code": DEPLOYED}),
        3,
        id="contract-deploy",
    ),
    pytest.param(
        (ALICE, "con_pool", "fail_after_swap", {"amount_in": 5}),
        4,
        id="failing-call-after-writes",
    ),
    pytest.param(
        (CAROL, "currency", "transfer", {"amount": 10**12, "to": BOB}),
        5,
        id="failing-transfer",
    ),
]


@pytest.mark.parametrize(("call", "nonce"), SCENARIOS)
def test_exact_estimate_executes_with_the_simulated_status_and_chi(tmp_path, call, nonce):
    chain = _setup(tmp_path)
    try:
        simulated, generous, exact = _simulate_then_execute(chain, call, nonce=nonce, height=1)
        _assert_exact_supply_executes_as_simulated(simulated, generous, exact)
    finally:
        chain[0].raw_driver.close()


def test_deployed_contract_call_executes_as_simulated(tmp_path):
    chain = _setup(tmp_path)
    try:
        deploy = (
            ALICE,
            "submission",
            "submit_contract",
            {"name": "con_deployed", "code": DEPLOYED},
        )
        _, _, deployed = _simulate_then_execute(chain, deploy, nonce=0, height=1)
        assert deployed["status"] == 0, deployed
        call = (BOB, "con_deployed", "set_value", {"new_value": 42})
        _assert_exact_supply_executes_as_simulated(
            *_simulate_then_execute(chain, call, nonce=1, height=2)
        )
    finally:
        chain[0].raw_driver.close()


def _decimal_text(rng, whole_digits):
    whole = rng.randrange(1, 10**whole_digits)
    fraction = str(rng.randrange(1, 10 ** rng.randrange(1, 9))).rstrip("0")
    return f"{whole}.{fraction}" if fraction else str(whole)


def _random_call(rng):
    sender = rng.choice((ALICE, BOB, CAROL))
    if rng.random() < 0.5:
        recipient = rng.choice(("d" * 64, ALICE, f"{rng.randrange(16**8):064x}"))
        amount = _decimal_text(rng, 3)
        return (sender, "currency", "transfer", {"amount": {"__fixed__": amount}, "to": recipient})
    return _swap(sender, _decimal_text(rng, 2))


def test_varied_amounts_balances_and_nonces_execute_as_simulated(tmp_path):
    # Amounts, balances and nonces change the encoded sizes that are charged
    # per byte, and the fee reservation changes the sender balance a call sees.
    chain = _setup(tmp_path)
    rng = random.Random(20261113)
    try:
        for height in range(1, 61):
            nonce = rng.randrange(0, 10 ** rng.randrange(1, 10))
            _assert_exact_supply_executes_as_simulated(
                *_simulate_then_execute(chain, _random_call(rng), nonce=nonce, height=height)
            )
    finally:
        chain[0].raw_driver.close()
