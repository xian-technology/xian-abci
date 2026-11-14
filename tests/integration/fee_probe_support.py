"""Shared fee-probe processor setup for the paid-execution integration tests."""

from pathlib import Path
from types import SimpleNamespace

from contracting.storage.driver import Driver

from xian.fee_policy import TxFeePolicy
from xian.processor import TxProcessor
from xian.utils.tx import transaction_bytes

ROOT = Path(__file__).resolve().parents[3]
PROBE = """import currency
value = Variable()
@export
def record():
    value.set(123)
    return currency.balance_of(address=ctx.caller)
@export
def spend(amount: decimal, fail: bool = False):
    value.set(456)
    currency.transfer_from(amount=amount, to='recipient', main_account=ctx.caller)
    assert not fail, 'deliberate failure'
"""
RATE = """S = Hash()
@export
def set_value(new_value: int):
    S['value'] = new_value
"""


def make_processor(home, *, balance=100, rate=100):
    driver = Driver(vm_profile="xian_vm_v2", storage_home=home)
    driver.set_contract_from_source(
        "currency", (ROOT / "xian-configs/contracts/currency.s.py").read_text()
    )
    driver.set_contract_from_source("con_probe", PROBE)
    driver.set_contract_from_source("chi_cost", RATE)
    for account in ("alice", "bob"):
        driver.set(f"currency.balances:{account}", balance)
        driver.set(f"currency.approvals:{account}:con_probe", 100)
    driver.set("chi_cost.S:value", rate)
    driver.set("foundation.owner", "foundation")
    driver.set("validators.active_validators", ["validator"])
    driver.set("rewards.S:value", [1, 0, 0, 0])
    driver.commit()
    client = SimpleNamespace(raw_driver=driver, get_var=driver.get_var)
    return TxProcessor(client=client)


def tx(*, sender="alice", contract="con_probe", function="record", kwargs=None, budget=1000):
    return {
        "payload": {
            "sender": sender,
            "contract": contract,
            "function": function,
            "kwargs": kwargs or {},
            "nonce": 0,
            "chi_supplied": budget,
        },
        "metadata": {"signature": sender + function},
        "b_meta": {"nanos": 1_000_000_000, "height": 1, "hash": "ab", "chain_id": "local"},
    }


def process(processor, transaction, *, policy=None):
    """The block executor's response for ``transaction`` alone in a block."""
    [response] = processor.process_block(
        [transaction],
        [transaction_bytes(transaction)],
        fee_policy=policy or TxFeePolicy.paid_metered(),
    )
    return response
