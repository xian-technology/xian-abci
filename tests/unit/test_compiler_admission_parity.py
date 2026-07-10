from __future__ import annotations

import json
from pathlib import Path

import pytest
import xian_compiler_core
from contracting.local import ContractingClient
from contracting.storage.driver import Driver, SOURCE_KEY, XIAN_VM_V1_IR_KEY
from xian_runtime_types.time import Datetime

from xian.execution_engine import (
    _load_vm_runtime_bindings,
    build_vm_runtime,
    clear_prepared_contract_cache,
    execute_vm_contract,
)

COMPILER_FIXTURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "xian-contracting"
    / "packages"
    / "xian-compiler-core"
    / "tests"
    / "fixtures"
)


def _deploy(
    module_name: str,
    source: str,
    *,
    constructor_args: dict[str, object] | None = None,
):
    _load_vm_runtime_bindings.cache_clear()
    clear_prepared_contract_cache()
    driver = Driver()
    driver.flush_full()
    ContractingClient(driver=driver)
    output = execute_vm_contract(
        build_vm_runtime(),
        driver,
        sender="sys",
        contract_name="submission",
        function_name="submit_contract",
        kwargs={
            "name": module_name,
            "code": source,
            "constructor_args": constructor_args or {},
        },
        environment={
            "now": Datetime(2026, 7, 10, 12, 0),
            "block_num": 7,
            "block_hash": "compiler-parity",
            "chain_id": "xian-local",
        },
        meter=False,
    )
    return driver, output


@pytest.mark.parametrize(
    "fixture_path",
    sorted(COMPILER_FIXTURE_DIR.glob("*.json")),
)
def test_node_admission_matches_shared_compiler_fixture(fixture_path: Path) -> None:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    constructor_args = (
        {"supply": 1} if fixture["module_name"] == "con_complex_ledger" else {}
    )
    driver, output = _deploy(
        fixture["module_name"],
        fixture["input_source"],
        constructor_args=constructor_args,
    )
    try:
        if fixture["expected"]["accepted"]:
            assert output.status_code == 0, output.result
            assert output.writes[
                driver.make_key(fixture["module_name"], SOURCE_KEY)
            ] == fixture["artifact"]["source"]
            assert output.writes[
                driver.make_key(fixture["module_name"], XIAN_VM_V1_IR_KEY)
            ] == fixture["artifact"]["vm_ir_json"]
        else:
            assert output.status_code == 1
            assert output.writes == {}
            primary = fixture["diagnostics"][0]
            assert primary["code"] in str(output.result)
            assert primary["message"] in str(output.result)
    finally:
        driver.flush_full()


def _limit_sources() -> list[tuple[str, str]]:
    limits = xian_compiler_core.compiler_version()["limits"]
    return [
        (
            "xian.limit.source_bytes",
            "a" * (limits["max_source_bytes"] + 1),
        ),
        (
            "xian.limit.tokens",
            "a=0\n" * ((limits["max_tokens"] // 4) + 1),
        ),
        (
            "xian.limit.logical_line_tokens",
            f"value = {'not ' * limits['max_logical_line_tokens']}True\n",
        ),
        (
            "xian.limit.syntax_nodes",
            "a=0\n" * ((limits["max_syntax_nodes"] // 3) + 1),
        ),
        (
            "xian.limit.syntax_depth",
            "@export\ndef value():\n    return "
            + ("not " * limits["max_syntax_depth"])
            + "True\n",
        ),
    ]


@pytest.mark.parametrize(("expected_code", "source"), _limit_sources())
def test_node_admission_enforces_compiler_limits(
    expected_code: str,
    source: str,
) -> None:
    driver, output = _deploy(f"con_{expected_code.rsplit('.', 1)[-1]}", source)
    try:
        assert output.status_code == 1
        assert output.writes == {}
        assert expected_code in str(output.result)
    finally:
        driver.flush_full()
