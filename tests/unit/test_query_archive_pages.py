from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from xian.methods._query_pages import (
    MAX_ARCHIVE_SCAN,
    contract_names_page,
    open_votes_page,
    parse_contract_cursor,
    stored_key_exists,
)


def test_sparse_vote_page_never_reads_more_than_one_work_budget():
    reads = []

    def get(key):
        reads.append(key)
        return 5000 if key == "validators.total_votes" else {"finalized": True}

    result = open_votes_page(SimpleNamespace(get=get), limit=25)

    assert result == {"items": [], "next_cursor": 5000 - MAX_ARCHIVE_SCAN, "limit": 25}
    assert len(reads) == MAX_ARCHIVE_SCAN + 1


def test_vote_page_keeps_pending_records_in_reverse_creation_order():
    state = {
        "validators.total_votes": 4,
        "validators.votes:4": {"status": "pending"},
        "validators.votes:3": {"status": "pending", "finalized": True},
        "validators.votes:2": "bad row",
        "validators.votes:1": {"status": "pending", "finalized": False},
    }
    driver = SimpleNamespace(get=state.get)

    first = open_votes_page(driver, limit=1)
    assert first["items"] == [{"status": "pending", "proposal_id": 4}]
    assert first["next_cursor"] == 3
    last = open_votes_page(driver, limit=1, cursor=first["next_cursor"])
    assert last["items"] == [{"status": "pending", "finalized": False, "proposal_id": 1}]
    assert last["next_cursor"] is None
    assert open_votes_page(SimpleNamespace(get=lambda key: 0), limit=25)["items"] == []
    with pytest.raises(ValueError, match="beyond"):
        open_votes_page(driver, limit=25, cursor=5)


@pytest.mark.parametrize("total", [None, 0])
def test_empty_proposal_archive_does_not_read_a_nonexistent_id(total):
    read = Mock(return_value=total)

    assert open_votes_page(SimpleNamespace(get=read), limit=25) == {
        "items": [], "next_cursor": None, "limit": 25,
    }
    read.assert_called_once_with("validators.total_votes")


def test_proposal_continuation_keeps_id_one_when_it_is_the_next_page():
    state = {"validators.total_votes": 2,
             "validators.votes:2": {"status": "pending"},
             "validators.votes:1": {"status": "pending"}}
    driver = SimpleNamespace(get=state.get)

    first = open_votes_page(driver, limit=1)

    assert first["next_cursor"] == 1
    last = open_votes_page(driver, limit=1, cursor=first["next_cursor"])
    assert [item["proposal_id"] for item in last["items"]] == [1]
    assert last["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["bad.name", "bad/name", "bad\x00name", "x" * 512, "%FF"])
def test_contract_cursor_rejects_invalid_namespace(cursor):
    with pytest.raises((ValueError, UnicodeError)):
        parse_contract_cursor(cursor)


def test_contract_cursor_round_trips_unicode_and_maximal_key():
    assert parse_contract_cursor(None) is None
    assert parse_contract_cursor("") is None
    assert parse_contract_cursor(quote("con_名字")) == "con_名字"
    assert parse_contract_cursor("x" * 511) == "x" * 511
    driver = SimpleNamespace(scan_keys_from_disk=Mock(return_value=([], False)))
    assert contract_names_page(driver, limit=1, after="x" * 511) == ([], None)
    driver.scan_keys_from_disk.assert_called_once_with("", limit=1, after_key="x" * 511)
    assert stored_key_exists(driver, "x" * 512) is False
    driver.scan_keys_from_disk.return_value = (["x" * 511], False)
    assert stored_key_exists(driver, "x" * 511) is True


def test_contract_namespace_page_seeks_past_large_noncontract_ranges():
    seen = []
    source = {"con_a.__source__", "con_a.items:a", "con_ab.__xian_ir_v1__", "con_b.__source__"}

    def scan(prefix, *, limit, after_key=None):
        seen.append((prefix, after_key))
        keys = sorted(key for key in source if key.startswith(prefix) and (after_key is None or key > after_key))
        return keys[:limit], len(keys) > limit

    driver = SimpleNamespace(scan_keys_from_disk=scan)
    first, after = contract_names_page(driver, limit=2)
    assert first == [("con_a", True), ("con_ab", False)]
    assert after == "con_ab"
    assert contract_names_page(driver, limit=2, after=after) == ([("con_b", True)], None)
    assert ("", "con_a/") in seen


def test_sparse_contract_page_stops_at_namespace_work_budget():
    keys = [f"state_{index:04}.item" for index in range(MAX_ARCHIVE_SCAN + 1)]
    reads = []

    def scan(prefix, *, limit, after_key=None):
        reads.append((prefix, after_key))
        found = [key for key in keys if key.startswith(prefix) and (after_key is None or key > after_key)]
        return found[:limit], len(found) > limit

    driver = SimpleNamespace(scan_keys_from_disk=scan)
    items, after = contract_names_page(driver, limit=25)

    assert items == []
    assert after == f"state_{MAX_ARCHIVE_SCAN - 1:04}"
    assert len(reads) == MAX_ARCHIVE_SCAN * 3
    assert contract_names_page(driver, limit=25, after=after) == ([], None)
