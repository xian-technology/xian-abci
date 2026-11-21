"""Run both SDKs against the same disposable V2 development node."""

import asyncio
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from xian_runtime_types.encoding import encode_wire

WORKSPACE = Path(__file__).resolve().parents[3]
XIAN_JS_DIR = Path(os.environ.get("XIAN_JS_DIR", WORKSPACE / "xian-js")).resolve()
RPC = os.environ.get("XIAN_LIVE_SDK_RPC")
pytestmark = pytest.mark.skipif(not RPC, reason="set XIAN_LIVE_SDK_RPC to a disposable dev node")

SOURCE = """memo = Variable()
saved_amount = Variable()

@export
def store(text: str, amount: decimal):
    memo.set(text)
    saved_amount.set(amount)
    return [amount, str(amount)]

@export
def echo(value: list):
    return value

@export
def balance_of(address: str):
    if ctx.caller == address:
        return decimal('3.5')
    return decimal('7')
"""
AMOUNT = "1.000000000000000000000000000001"
MEMO = "\ufeffe\u0301 / 雪 / 😀"
PRIVATE_KEY = "01" * 32
OWNER = "02" * 32


def test_sdk_contract_values_and_receipts():
    from xian_py import Wallet, XianAsync
    from xian_py.exception import RpcError, TxNotFoundError
    from xian_py.transaction import prepare_transaction

    async def exercise():
        contract = f"con_sdk_parity_{uuid4().hex}"
        async with XianAsync(RPC, wallet=Wallet(PRIVATE_KEY)) as client:
            deployed = await client.deploy_contract(contract, SOURCE, chi=190_000, wait_for_tx=True)
            assert deployed.receipt and deployed.receipt.success, deployed
            sent = await client.send_tx(
                contract, "store", {"text": MEMO, "amount": Decimal(AMOUNT)}, chi=20_000
            )
            assert sent.accepted, sent
            receipt = await client.wait_for_tx(sent.tx_hash)
            assert receipt.success, receipt
            amount = await client.get_state(contract, "saved_amount")
            echoed = await client.send_tx(
                contract, "echo", {"value": [amount, AMOUNT]}, chi=20_000, wait_for_tx=True
            )
            assert echoed.receipt and echoed.receipt.success, echoed
            with pytest.raises((RpcError, TxNotFoundError)) as missing:
                await client.get_tx("CD" * 32)
            return {
                "contract": contract,
                "receipt": {
                    "hash": receipt.tx_hash,
                    "wire": prepare_transaction(receipt.transaction).tx_hash,
                    "result": receipt.execution["result"],
                },
                "memo": await client.get_state(contract, "memo"),
                "amount": amount,
                "echoed": echoed.receipt.execution["result"],
                "balance": await client.get_balance(OWNER, contract),
                "allowance": await client.token(contract).allowance("con_spender", owner=OWNER),
                "missingTx": type(missing.value).__name__,
            }

    python = json.loads(encode_wire(asyncio.run(exercise())))
    tag = {"__fixed__": AMOUNT}
    assert python["memo"] == MEMO
    assert python["amount"] == tag
    assert python["echoed"] == python["receipt"]["result"] == [tag, AMOUNT]
    assert python["receipt"]["wire"] == python["receipt"]["hash"]

    js = subprocess.run(
        ["node", str(Path(__file__).with_name("live-sdk-parity.mjs"))],
        cwd=XIAN_JS_DIR,
        input=json.dumps(
            {
                "rpc": RPC,
                "contract": python["contract"],
                "hash": python["receipt"]["hash"],
                "owner": OWNER,
                "key": PRIVATE_KEY,
            }
        ),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert js.returncode == 0, js.stderr
    assert json.loads(js.stdout) == python
    assert python["balance"] == {"__fixed__": "3.5"}
    assert python["allowance"] == 0
