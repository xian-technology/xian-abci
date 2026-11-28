"""Static transaction admission against its golden parity corpus.

CheckTx, PrepareProposal, ProcessProposal and FinalizeBlock admit a
transaction through one Rust implementation in ``xian_vm_core``. The corpus
(``admission_corpus.py``) freezes what admission does with every case; the
node and the binding must reproduce it exactly.
"""

from __future__ import annotations

import pytest
import xian_vm_core
from admission_corpus import CHAIN_ID, cases, load_fixture, node_admission, outcome, run

import xian.utils.tx as tx_utils

GOLDEN = load_fixture()


def test_admission_cache_requires_the_native_check_tx_api(monkeypatch):
    monkeypatch.setattr(xian_vm_core, "TransactionAdmissionCache", object)
    with pytest.raises(RuntimeError, match="does not provide native CheckTx admission"):
        tx_utils.create_transaction_admission_cache(CHAIN_ID)


def _assert_matches_golden(actual: dict[str, dict]) -> None:
    assert set(actual) == set(GOLDEN)
    mismatches = {
        name: (actual[name], GOLDEN[name]) for name in GOLDEN if actual[name] != GOLDEN[name]
    }
    assert not mismatches, mismatches


def test_the_corpus_is_the_one_the_fixture_froze():
    assert {name: case["raw"] for name, case in run(lambda raw: None).items()} == {
        name: case["raw"] for name, case in GOLDEN.items()
    }


def test_node_admission_matches_the_golden_corpus():
    _assert_matches_golden(run(node_admission))


def test_the_vm_core_binding_matches_the_golden_corpus():
    _assert_matches_golden(
        run(lambda raw: xian_vm_core.decode_and_validate_transaction(raw, CHAIN_ID))
    )


@pytest.fixture
def native_cache_type():
    cache_type = getattr(xian_vm_core, "TransactionAdmissionCache", None)
    if cache_type is None:
        pytest.skip("the installed native VM wheel predates admission caching")
    return cache_type


def test_native_admission_cache_matches_cold_warm_and_disabled_golden_corpus(native_cache_type):
    def node_cached_admission(raw, cache):
        return tx_utils.decode_and_validate_transaction_static_bytes(
            raw, chain_id=CHAIN_ID, admission_cache=cache
        )

    disabled = native_cache_type(CHAIN_ID, max_entries=0, max_bytes=0)
    _assert_matches_golden(run(lambda raw: node_cached_admission(raw, disabled)))
    assert disabled.stats()["hits"] == 0

    cache = native_cache_type(CHAIN_ID, max_entries=256, max_bytes=8 * 1024 * 1024)
    _assert_matches_golden(run(lambda raw: node_cached_admission(raw, cache)))
    first_pass_hits = cache.stats()["hits"]
    _assert_matches_golden(run(lambda raw: node_cached_admission(raw, cache)))
    assert cache.stats()["hits"] > first_pass_hits


def test_raw_size_and_chain_are_checked_before_using_a_warm_cache(native_cache_type):
    cache = native_cache_type(CHAIN_ID, max_entries=256, max_bytes=8 * 1024 * 1024)
    raw = cases()[0][1]
    assert cache.decode(raw) == node_admission(raw)

    for chain_id, limit, refusal in [
        (CHAIN_ID, len(raw) - 1, "Transaction exceeds maximum configured size"),
        ("another-chain", len(raw), "Wrong chain_id"),
    ]:
        with pytest.raises(tx_utils.TransactionException, match=refusal):
            tx_utils.decode_and_validate_transaction_static_bytes(
                raw, chain_id=chain_id, max_raw_tx_bytes=limit, admission_cache=cache
            )
    assert cache.stats()["hits"] == 0


def test_every_refusal_is_a_transaction_exception():
    for name, raw in cases():
        kind, text = GOLDEN[name]["outcome"]
        if kind == "error":
            with pytest.raises(tx_utils.TransactionException) as refused:
                node_admission(raw)
            assert str(refused.value) == text, name


def test_admission_has_no_python_fallback():
    for name in (
        "validate_transaction_static",
        "check_tx_formatting",
        "recurse_rules",
        "verify",
        "_native_decode_and_validate_transaction_static",
        "_require_canonical_identity",
        "_validate_wire_payload",
    ):
        assert not hasattr(tx_utils, name), name


@pytest.mark.parametrize("limit", [10, 999])
def test_the_configured_size_limit_applies_before_decoding(limit):
    raw = cases()[0][1]
    result = outcome(
        lambda data: tx_utils.decode_and_validate_transaction_static_bytes(
            data, chain_id=CHAIN_ID, max_raw_tx_bytes=limit
        ),
        raw,
    )
    if len(raw) > limit:
        assert result == ["error", f"Transaction exceeds maximum configured size of {limit} bytes"]
    else:
        assert result == GOLDEN[cases()[0][0]]["outcome"]
