from types import SimpleNamespace

import pytest
from contracting.compilation.linter import LintingError
from contracting.execution.executor import Executor
from contracting.storage.driver import Driver

from xian.state_export import build_exported_state, import_state
from xian.state_root import StateRootCache, compute_exported_state_root
from xian.validators import CONSENSUS_VALIDATORS_KEY, ValidatorHandler

INCUMBENT = "ab" * 32
REPLACEMENT = "cd" * 32


def handler(driver):
    return ValidatorHandler(SimpleNamespace(client=SimpleNamespace(raw_driver=driver)))


def test_effective_set_survives_commit_restart_and_state_export_import(tmp_path):
    original = tmp_path / "original"
    with Driver(storage_home=original) as driver:
        driver.set("validators.active_validators", [INCUMBENT])
        driver.commit()
        state_root = StateRootCache.from_driver(driver)
        driver.set("validators.active_validators", ["con_invalid"])
        assert handler(driver).build_validator_updates(1) == []
        assert driver.value_from_disk(CONSENSUS_VALIDATORS_KEY) is None
        prepared_root = state_root.prepare(driver.pending_writes)
        driver.commit()
        assert StateRootCache.from_driver(driver).root_hash == prepared_root

    with Driver(storage_home=original) as restarted:
        assert restarted.get("validators.active_validators") == ["con_invalid"]
        assert restarted.get(CONSENSUS_VALIDATORS_KEY) == {INCUMBENT: 10}
        assert handler(restarted).build_validator_updates(2) == []
        exported = build_exported_state(
            founder_private_key=None,
            contract_state=restarted.get_all_contract_state(),
            run_state=restarted.get_run_state(),
            latest_block_hash=prepared_root,
            latest_block_height=1,
            latest_block_nanos=1,
        )

    exported_entries = {item["key"]: item["value"] for item in exported["genesis"]}
    assert exported_entries[CONSENSUS_VALIDATORS_KEY] == {INCUMBENT: 10}
    without_baseline = {
        **exported,
        "genesis": [
            item for item in exported["genesis"] if item["key"] != CONSENSUS_VALIDATORS_KEY
        ],
    }
    assert compute_exported_state_root(without_baseline) != bytes.fromhex(exported["hash"])
    with pytest.raises(ValueError, match="state root mismatch"):
        import_state(exported_state=without_baseline, storage_home=tmp_path / "tampered")

    restored = tmp_path / "restored"
    import_state(exported_state=exported, storage_home=restored)
    with Driver(storage_home=restored) as driver:
        driver.set("validators.active_validators", [REPLACEMENT.upper()])
        updates = handler(driver).build_validator_updates(3)
        assert [(item.pub_key.ed25519.hex(), item.power) for item in updates] == [
            (REPLACEMENT, 10),
            (INCUMBENT, 0),
        ]
        driver.commit()
    with Driver(storage_home=restored) as driver:
        assert handler(driver).build_validator_updates(4) == []
        assert driver.get(CONSENSUS_VALIDATORS_KEY) == {REPLACEMENT: 10}


def test_consensus_metadata_name_cannot_be_declared_by_contracts(tmp_path):
    with Driver(storage_home=tmp_path) as driver:
        with pytest.raises(LintingError, match="must not start or end with underscore"):
            driver.set_contract_from_source(
                "con_forgery",
                "__consensus__ = Variable()\n@export\ndef write():\n    __consensus__.set({})\n",
                owner=None,
            )


@pytest.mark.parametrize("native", [False, True])
def test_foreign_contract_cannot_write_effective_consensus_metadata(tmp_path, native):
    with Driver(storage_home=tmp_path) as driver:
        driver.set_contract_from_source(
            "validators", "@export\ndef ping():\n    return True\n", owner=None
        )
        driver.set(CONSENSUS_VALIDATORS_KEY, {INCUMBENT: 10})
        driver.set_contract_from_source(
            "con_forgery",
            "value = ForeignVariable(foreign_contract='validators', foreign_name='__consensus__')\n"
            "@export\ndef write():\n    value.set({})\n",
            owner=None,
        )
        driver.commit()
        if native:
            vm = pytest.importorskip("xian_vm_core")
            result = vm.execute_contract(
                driver=driver,
                contract_name="con_forgery",
                function_name="write",
                kwargs={},
                context={
                    "signer": "alice",
                    "caller": "alice",
                    "this": "con_forgery",
                    "owner": None,
                },
                meter=False,
                chi_budget_raw=1_000_000,
            )
            assert result.status_code == 1
            assert result.writes == {}
        else:
            result = Executor(driver=driver, metering=False).execute(
                sender="alice",
                contract_name="con_forgery",
                function_name="write",
                kwargs={},
                metering=False,
            )
            assert result["status_code"] == 1
            assert result["writes"] == {}
        assert driver.get(CONSENSUS_VALIDATORS_KEY) == {INCUMBENT: 10}
