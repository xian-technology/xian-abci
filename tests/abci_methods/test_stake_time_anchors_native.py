"""Actual canonical native clock anchoring through FinalizeBlock/Commit."""

import asyncio
from datetime import UTC, datetime

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from xian_runtime_types.time import Datetime

from tendermint.abci.types_pb2 import RequestFinalizeBlock
from xian.config_paths import resolve_contracts_dir
from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
from xian.state_root import compute_driver_state_root
from xian.utils.block import stage_latest_block
from xian.xian_abci import Xian

NODE = "11" * 32
OTHER = "22" * 32


@pytest.fixture
def native_chain(node_fixture, node_constants):
    chain = {"app": Xian(constants=node_constants)}
    try:
        app = chain["app"]
        directory = resolve_contracts_dir()
        for name, args in [
            ("currency", {"vk": "founder"}),
            ("dao", {}),
            ("rewards", {}),
            ("chi_cost", {"initial_rate": 20}),
            ("validator_penalties", {}),
            (
                "validators",
                {
                    "genesis_nodes": [NODE, OTHER],
                    "genesis_registration_fee": 1000,
                    "unbonding_period_days": 14,
                },
            ),
        ]:
            app.client.submit(
                (directory / f"{name}.s.py").read_text(), name=name, constructor_args=args
            )
        currency = app.client.get_contract_proxy("currency")
        validators = app.client.get_contract_proxy("validators")
        currency.transfer(amount=40, to=NODE, signer="founder")
        environment = {"block_num": 100, "now": Datetime(2026, 1, 1)}
        currency.approve(amount=40, to="validators", signer=NODE, environment=environment)
        validators.bond_self(amount=40, signer=NODE, environment=environment)
        app.client.raw_driver.set(EVIDENCE_POLICY_KEY, EvidencePolicy.seed(14, nanos=0).to_state())
        chain.update(currency=currency, validators=validators, environment=environment)
        yield chain
    finally:
        asyncio.run(chain["app"].close())
        chain["app"].client.close()


def _checkpoint(app):
    stale = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    root = compute_driver_state_root(app.client.raw_driver)
    stage_latest_block(
        app.client.raw_driver, block_hash=root, height=100, nanos=stale * 1_000_000_000
    )
    app.client.raw_driver.commit()
    app.state_root_cache.rebuild_from_driver(app.client.raw_driver)


def _empty(app, height, seconds):
    response = asyncio.run(
        app.finalize_block(
            RequestFinalizeBlock(height=height, time=Timestamp(seconds=seconds), txs=[])
        )
    )
    asyncio.run(app.commit())
    return response


def test_native_unbond_resumes_after_restart_and_cannot_collapse_after_idle(
    native_chain, node_constants
):
    app = native_chain["app"]
    unbond = native_chain["validators"].unbond_self(
        amount=1, signer=NODE, environment=native_chain["environment"]
    )
    assert unbond["unlock_at"] is None
    _checkpoint(app)
    asyncio.run(app.close())
    app.client.close()
    app = Xian(constants=node_constants)
    native_chain["app"] = app
    currency = app.client.get_contract_proxy("currency")
    validators = app.client.get_contract_proxy("validators")
    fresh = int(datetime(2026, 4, 11, tzinfo=UTC).timestamp())
    anchored = _empty(app, 101, fresh)
    record = validators.get_pending_unbond(unbond_id=unbond["id"])
    assert record["unlock_at"] == Datetime(2026, 4, 25)
    assert record["created_at"] == Datetime(2026, 1, 1) and record["created_block"] == 100
    assert app.client.raw_driver.get("validator_penalties.time_anchor_head") is None
    assert validators.get_validator(account=NODE)["next_unbond_unlock_at"] == Datetime(2026, 4, 25)
    assert _empty(app, 102, fresh + 1).app_hash == anchored.app_hash
    with pytest.raises(AssertionError, match="locked"):
        validators.claim_unbond(
            unbond_id=unbond["id"],
            signer=NODE,
            environment={"block_num": 103, "now": Datetime(2026, 4, 11)},
        )
    assert currency.balance_of(address=NODE) == 0
    validators.claim_unbond(
        unbond_id=unbond["id"],
        signer=NODE,
        environment={"block_num": 104, "now": Datetime(2026, 4, 25)},
    )
    assert currency.balance_of(address=NODE) == 1


def test_native_budget_advances_only32_then_resumes_and_quiesces(native_chain):
    app = native_chain["app"]
    validators = native_chain["validators"]
    requests = [
        validators.unbond_self(amount=1, signer=NODE, environment=native_chain["environment"])
        for _ in range(33)
    ]
    _checkpoint(app)
    fresh = int(datetime(2026, 4, 11, tzinfo=UTC).timestamp())
    first = _empty(app, 101, fresh)
    assert app.client.raw_driver.get("validator_penalties.time_anchor_head") == 33
    assert validators.get_pending_unbond(unbond_id=requests[31]["id"])["unlock_at"] == Datetime(
        2026, 4, 25
    )
    assert validators.get_pending_unbond(unbond_id=requests[32]["id"])["unlock_at"] is None
    last = _empty(app, 102, fresh + 1)
    assert app.client.raw_driver.get("validator_penalties.time_anchor_head") is None
    assert validators.get_pending_unbond(unbond_id=requests[32]["id"])["unlock_at"] == Datetime(
        2026, 4, 25, 0, 0, 1
    )
    assert last.app_hash != first.app_hash
    assert _empty(app, 103, fresh + 2).app_hash == last.app_hash
