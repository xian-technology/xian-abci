import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xian_accounts import Ed25519Account

from xian.constants import Constants
from xian.genesis_builder import build_single_validator_genesis
from xian.node_setup import (
    NodeConfigOptions,
    generate_validator_material,
    materialize_cometbft_home,
    render_node_configs,
)
from xian.utils.block import store_genesis_block
from xian.xian_abci import Xian

FOUNDER_PRIVATE_KEY = (
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
FOUNDER_PUBLIC_KEY = Ed25519Account(FOUNDER_PRIVATE_KEY).public_key
RECIPIENT = "f" * 64


def _fresh_runtime_constants(home: Path):
    class RuntimeConstants(Constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config" / "config.toml"
        XIAN_CONFIG = home / "config" / "xian.toml"
        COMETBFT_GENESIS = home / "config" / "genesis.json"
        STORAGE_HOME = home / "xian"

    return RuntimeConstants


def _approval_tx(*, chain_id: str, chi_supplied: int = 100_000) -> dict:
    nanos = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    return {
        "payload": {
            "chain_id": chain_id,
            "contract": "currency",
            "function": "approve",
            "sender": FOUNDER_PUBLIC_KEY,
            "kwargs": {"amount": 7, "to": RECIPIENT},
            "nonce": 1,
            "chi_supplied": chi_supplied,
        },
        "metadata": {"signature": "integration-test"},
        "b_meta": {
            "height": 1,
            "nanos": nanos,
            "chain_id": chain_id,
            "hash": "fresh-network-test",
        },
    }


class FeePolicyFreshNetworkSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_network_fee_modes_boot_and_execute_as_configured(self):
        contracts_dir = (
            Path(__file__).resolve().parents[3] / "xian-configs" / "contracts"
        )
        cases = (
            {
                "mode": "free_metered",
                "chain_id": "xian-free-metered-test-1",
                "expected_charge_fees": False,
                "expected_balance_check": False,
            },
            {
                "mode": "paid_metered",
                "chain_id": "xian-paid-metered-test-1",
                "expected_charge_fees": True,
                "expected_balance_check": True,
            },
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            for index, case in enumerate(cases, start=1):
                home = Path(tmp_dir) / case["mode"] / ".cometbft"
                validator_material = generate_validator_material(f"{index:064x}")
                genesis = build_single_validator_genesis(
                    chain_id=case["chain_id"],
                    priv_validator_key=validator_material["priv_validator_key"],
                    founder_private_key=FOUNDER_PRIVATE_KEY,
                    network="local",
                    validator_name=f"validator-{index}",
                    contracts_dir=contracts_dir,
                )
                configs = render_node_configs(
                    options=NodeConfigOptions(
                        moniker=f"validator-{index}",
                        tx_fee_mode=case["mode"],
                        free_tx_max_chi=250_000,
                        free_block_max_chi=1_000_000,
                    )
                )
                materialize_cometbft_home(
                    home=home,
                    config=configs["cometbft"],
                    xian_config=configs["xian"],
                    genesis=genesis,
                    priv_validator_key=validator_material["priv_validator_key"],
                )

                app = await Xian.create(constants=_fresh_runtime_constants(home))
                try:
                    app.client.raw_driver.flush_full()
                    await store_genesis_block(
                        app.client,
                        app.nonce_storage,
                        app.genesis["abci_genesis"],
                    )
                    state_root = app.state_root_cache.rebuild(
                        app.client.raw_driver.items().items()
                    )
                    self.assertEqual(
                        state_root.hex(),
                        app.genesis["abci_genesis"]["hash"],
                    )

                    self.assertEqual(app.tx_fee_policy.mode, case["mode"])
                    self.assertTrue(app.tx_fee_policy.meter_execution)
                    self.assertEqual(
                        app.tx_fee_policy.charge_fees,
                        case["expected_charge_fees"],
                    )
                    self.assertEqual(
                        app.tx_fee_policy.require_chi_balance,
                        case["expected_balance_check"],
                    )
                    self.assertEqual(
                        app.enable_tx_fee,
                        case["expected_charge_fees"],
                    )
                    self.assertEqual(
                        app.simulator.charge_fees,
                        case["expected_charge_fees"],
                    )

                    balance_key = f"currency.balances:{FOUNDER_PUBLIC_KEY}"
                    starting_balance = app.client.raw_driver.get(balance_key)
                    result = app.tx_processor.process_tx(
                        tx=_approval_tx(chain_id=case["chain_id"]),
                        fee_policy=app.tx_fee_policy,
                    )
                    ending_balance = app.client.raw_driver.get(balance_key)

                    self.assertEqual(result["tx_result"]["status"], 0)
                    self.assertGreater(result["tx_result"]["chi_used"], 0)
                    self.assertEqual(
                        app.client.raw_driver.get(
                            f"currency.approvals:{FOUNDER_PUBLIC_KEY}:{RECIPIENT}"
                        ),
                        7,
                    )
                    if case["expected_charge_fees"]:
                        self.assertLess(ending_balance, starting_balance)
                    else:
                        self.assertEqual(ending_balance, starting_balance)
                finally:
                    await app.close()


if __name__ == "__main__":
    unittest.main()
