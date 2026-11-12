"""Contract preparation reuses a module's check only while its IR is unchanged.

Every transaction checks the whole import graph of its contract against
current state. The per-module check is reused for identical IR text, so a
changed module (or import) must be checked again at once.
"""

import pytest
from contracting.storage.driver import Driver

from xian.execution_engine import ContractPreparationError, VmRuntime, prepare_vm_contract

CHILD = "@export\ndef ping():\n    return 1\n"


def _importer(name):
    return f"import {name}\n@export\ndef ping():\n    return {name}.ping()\n"


@pytest.fixture
def driver(tmp_path):
    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set_contract_from_source("con_child", CHILD)
        driver.set_contract_from_source("con_parent", _importer("con_child"))
        yield driver


def test_preparation_follows_a_changed_import(driver):
    prepared = prepare_vm_contract(VmRuntime(), driver, "con_parent")
    assert prepared.imported_contracts == ("con_child",)

    driver.set_contract_from_source("con_other", CHILD)
    driver.set_contract_from_source("con_child", _importer("con_other"), overwrite=True)
    driver.delete("con_other.__xian_ir_v1__")
    driver.delete("con_other.__source__")

    with pytest.raises(ContractPreparationError, match="con_other"):
        prepare_vm_contract(VmRuntime(), driver, "con_parent")

    driver.set_contract_from_source("con_child", CHILD, overwrite=True)
    assert prepare_vm_contract(VmRuntime(), driver, "con_parent").contract_name == "con_parent"


def test_preparation_reports_the_current_imports(driver):
    prepare_vm_contract(VmRuntime(), driver, "con_parent")
    driver.set_contract_from_source("con_other", CHILD)

    driver.set_contract_from_source("con_parent", _importer("con_other"), overwrite=True)

    assert prepare_vm_contract(VmRuntime(), driver, "con_parent").imported_contracts == (
        "con_other",
    )
