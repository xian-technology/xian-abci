"""Tests for shielded output tag extraction in the BDS indexer."""

import json

from xian.services.bds.shielded import (
    collect_shielded_output_tags,
    extract_payload_tags,
)


def _payload_hex(ciphertexts) -> str:
    return "0x" + json.dumps({"ciphertexts": ciphertexts}).encode("utf-8").hex()


def test_extract_payload_tags_collects_and_dedupes_tag_kinds():
    payload = _payload_hex(
        [
            {"sync_hint": "hint-a", "discovery_tag": "tag-a"},
            {"sync_hint": "hint-a"},
            {"sync_hint": "", "discovery_tag": 7},
            "not-a-dict",
        ]
    )

    tags = extract_payload_tags(payload)

    assert tags == [
        {"tag_kind": "sync_hint", "tag_value": "hint-a"},
        {"tag_kind": "discovery_tag", "tag_value": "tag-a"},
    ]


def test_extract_payload_tags_tolerates_malformed_payloads():
    assert extract_payload_tags(None) == []
    assert extract_payload_tags("") == []
    assert extract_payload_tags("0xzznothex") == []
    assert extract_payload_tags("0x" + b"[1, 2]".hex()) == []
    assert (
        extract_payload_tags("0x" + b'{"ciphertexts": 5}'.hex()) == []
    )


def _collect(events, output_payloads):
    return collect_shielded_output_tags(
        contract="con_token",
        function="transfer_shielded",
        tx_hash="AB" * 32,
        block_height=7,
        tx_index=0,
        tx_result_events=events,
        kwargs={"output_payloads": output_payloads},
    )


def test_collect_tags_for_single_output_event():
    payload = _payload_hex([{"sync_hint": "hint-a"}])
    events = [
        "not-a-dict",
        {"contract": "con_other", "event": "ShieldedOutputCommitted"},
        {"contract": "con_token", "event": "Transfer"},
        {
            "contract": "con_token",
            "event": "ShieldedOutputCommitted",
            "data_indexed": {"commitment": "0xcommit", "new_root": "0xroot"},
            "data": {
                "output_index": 0,
                "note_index": 12,
                "payload_hash": "0xhash",
                "action": "transfer",
            },
        },
        {
            "contract": "con_token",
            "event": "ShieldedOutputCommitted",
            "data": {"output_index": "not-an-int"},
        },
    ]

    rows = _collect(events, [payload])

    assert rows == [
        {
            "tx_hash": "AB" * 32,
            "block_height": 7,
            "tx_index": 0,
            "contract": "con_token",
            "function": "transfer_shielded",
            "action": "transfer",
            "output_index": 0,
            "note_index": 12,
            "commitment": "0xcommit",
            "new_root": "0xroot",
            "payload_hash": "0xhash",
            "tag_kind": "sync_hint",
            "tag_value": "hint-a",
        }
    ]


def test_collect_tags_for_batched_outputs_event():
    payloads = [
        _payload_hex([{"sync_hint": "hint-0"}]),
        _payload_hex([{"discovery_tag": "tag-1"}]),
    ]
    events = [
        {
            "contract": "con_token",
            "event": "ShieldedOutputsCommitted",
            "data_indexed": {"new_root": "0xroot"},
            "data": {
                "note_index_start": 100,
                "commitments_blob": "0xc0|0xc1",
                "payload_hashes_blob": "0xh0|0xh1",
            },
        }
    ]

    rows = _collect(events, payloads)

    assert [
        (row["output_index"], row["note_index"], row["commitment"], row["tag_value"])
        for row in rows
    ] == [
        (0, 100, "0xc0", "hint-0"),
        (1, 101, "0xc1", "tag-1"),
    ]
    # output_count defaults to the commitment count when absent.
    assert all(row["payload_hash"] in {"0xh0", "0xh1"} for row in rows)


def test_collect_tags_skips_inconsistent_batched_events():
    payload = _payload_hex([{"sync_hint": "hint-a"}])
    events = [
        {
            "contract": "con_token",
            "event": "ShieldedOutputsCommitted",
            "data": {
                "note_index_start": "not-an-int",
                "commitments_blob": "0xc0",
                "payload_hashes_blob": "0xh0",
            },
        },
        {
            "contract": "con_token",
            "event": "ShieldedOutputsCommitted",
            "data": {
                "note_index_start": 100,
                "output_count": 2,
                "commitments_blob": "0xc0",
                "payload_hashes_blob": "0xh0",
            },
        },
    ]

    assert _collect(events, [payload]) == []


def test_collect_tags_ignores_out_of_range_outputs_and_empty_payloads():
    event = {
        "contract": "con_token",
        "event": "ShieldedOutputCommitted",
        "data": {"output_index": 5},
    }

    assert _collect([event], [_payload_hex([{"sync_hint": "x"}])]) == []
    assert _collect([event], []) == []
    assert _collect([event], "not-a-list") == []
