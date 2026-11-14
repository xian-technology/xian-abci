"""The engine parity corpora on both paths against the golden fixtures.

``engine_parity_corpus`` runs every corpus on the Python per-transaction
path and on the production path. The production path must reproduce the
frozen outputs in ``tests/fixtures/engine_parity/`` byte for byte (receipt
bytes, which carry the writes, chi, rewards and events; the block writes;
the app hash) and run every block transaction in Rust. The Python path must
reproduce them up to the first case execution revision 14 decides by a rule
it does not have (``REVISION_14_CASES``); once it is deleted, only the
golden comparison of the production path remains.
"""

from __future__ import annotations

import pytest
from engine_parity_corpus import (
    PER_TX_PATH,
    REVISION_14_CASES,
    before_revision_14_cases,
    SYSTEM_CASES,
    corpora,
    handback_inventory,
    handed_back,
    load_fixture,
    run_contract_views,
    run_corpus,
    run_simulations,
    run_system_blocks,
)

pytest.importorskip("xian_vm_core")

CORPORA = {corpus.name: corpus for corpus in corpora()}


def _corpus(name: str):
    # Every corpus runs: the VM links the zk verifier.
    return CORPORA[name]


def _assert_blocks_equal(actual: dict, expected: dict, path: str) -> None:
    assert actual["policy"] == expected["policy"]
    assert len(actual["blocks"]) == len(expected["blocks"]), path
    for block, golden in zip(actual["blocks"], expected["blocks"], strict=True):
        for receipt, golden_receipt in zip(
            block.get("txs", []), golden.get("txs", []), strict=True
        ):
            assert receipt == golden_receipt, f"{path}: {golden_receipt['case']}"
        assert block == golden, f"{path}: block {golden['height']}"


@pytest.mark.parametrize("name", sorted(CORPORA))
def test_block_corpus_matches_the_golden_fixture_on_both_paths(tmp_path, name):
    corpus = _corpus(name)
    golden = load_fixture(f"blocks_{name}")
    python, python_routes = run_corpus(tmp_path / "python", corpus, python=True)
    production, routes = run_corpus(tmp_path / "production", corpus, python=False)

    assert set(python_routes.values()) == {PER_TX_PATH}
    _assert_blocks_equal(
        before_revision_14_cases(python), before_revision_14_cases(golden), "python path"
    )
    _assert_blocks_equal(production, golden, "production path")
    assert handed_back(routes) == {}
    assert load_fixture("handback_inventory")[name] == {}


def test_the_inventory_lists_every_hand_back_and_outside_entry_point(tmp_path):
    assert handback_inventory(tmp_path) == load_fixture("handback_inventory")


@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_simulations_match_the_golden_fixture(tmp_path, python):
    results, routes = run_simulations(tmp_path, python=python)

    golden = load_fixture("simulations")
    if python:
        # The Python path keeps its old outcome for revision 14's cases.
        golden = {case: result for case, result in golden.items() if case not in REVISION_14_CASES}
        results = {case: results[case] for case in golden}
    assert results == golden
    if python:
        assert set(routes.values()) == {PER_TX_PATH}
    else:
        assert handed_back(routes) == load_fixture("handback_inventory")["simulations"]


@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_contract_views_match_the_golden_fixture(tmp_path, python):
    results, routes = run_contract_views(tmp_path, python=python)

    assert results == load_fixture("contract_views")
    if python:
        assert set(routes.values()) == {PER_TX_PATH}
    else:
        assert handed_back(routes) == load_fixture("handback_inventory")["contract_views"]


@pytest.mark.usefixtures("node_home")
@pytest.mark.parametrize("python", [True, False], ids=["python", "production"])
def test_system_transactions_match_the_golden_fixture(python):
    outputs, routes = run_system_blocks(python=python)

    assert outputs == load_fixture("system_blocks")
    if python:
        assert set(routes.values()) == {PER_TX_PATH}
        assert set(routes) == set(SYSTEM_CASES.values())
    else:
        assert routes == load_fixture("handback_inventory")["system_blocks"]
