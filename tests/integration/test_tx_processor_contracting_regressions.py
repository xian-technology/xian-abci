from __future__ import annotations

from datetime import UTC, datetime

from contracting.client import ContractingClient

from xian.processor import TxProcessor

CURRENCY_SOURCE = """
balances = Hash()

@construct
def seed():
    balances['stu'] = 1000000
    balances['colin'] = 100

@export
def transfer(amount: int, to: str):
    sender = ctx.signer
    assert balances[sender] >= amount, 'Not enough coins to send!'

    balances[sender] -= amount

    if balances[to] is None:
        balances[to] = amount
    else:
        balances[to] += amount
"""


EXCEPTION_SOURCE = """
balances = Hash(default_value=0)

@construct
def seed():
    balances['stu'] = 999
    balances['colin'] = 555

@export
def transfer(amount: int, to: str):
    sender = ctx.caller
    assert balances[sender] >= amount, 'Not enough coins to send!'

    balances[sender] -= amount
    balances[to] += amount

    raise Exception('This is an exception')
"""


def block_meta() -> dict:
    now = datetime.now(UTC)
    return {
        "nanos": int(now.timestamp() * 1_000_000_000),
        "hash": "0x0",
        "height": 1,
        "chain_id": "xian-test",
    }


def tx(contract: str, *, amount: int = 100) -> dict:
    return {
        "payload": {
            "sender": "stu",
            "contract": contract,
            "function": "transfer",
            "kwargs": {"amount": amount, "to": "colin"},
            "chi_supplied": 1000,
        },
        "metadata": {"signature": "abc"},
        "b_meta": block_meta(),
    }


def test_failed_contract_transaction_does_not_apply_contract_writes(tmp_path):
    client = ContractingClient(storage_home=tmp_path)
    client.flush()
    client.submit(EXCEPTION_SOURCE, name="con_exception")
    processor = TxProcessor(client=client)

    prior_balance = client.raw_driver.get("con_exception.balances:stu")

    result = processor.process_tx(tx("con_exception"))

    assert result["tx_result"]["status"] == 1
    assert client.raw_driver.get("con_exception.balances:stu") == prior_balance
    assert not any(
        write["key"].startswith("con_exception.")
        for write in result["tx_result"]["state"]
    )


def test_successful_contract_transaction_applies_contract_writes(tmp_path):
    client = ContractingClient(storage_home=tmp_path)
    client.flush()
    client.submit(CURRENCY_SOURCE, name="con_currency")
    processor = TxProcessor(client=client)

    prior_balance = client.raw_driver.get("con_currency.balances:stu")

    result = processor.process_tx(tx("con_currency"))

    assert result["tx_result"]["status"] == 0
    assert (
        client.raw_driver.get("con_currency.balances:stu")
        == prior_balance - 100
    )
