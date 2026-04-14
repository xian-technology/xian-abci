import unittest

from xian.legacy_network_replay import (
    _build_legacy_exported_state,
    _build_runtime_code_inventory,
    _coerce_legacy_value,
    _historical_record_from_graphql_node,
    _state_patch_record_from_graphql_node,
    _mismatch_fields,
    _normalize_legacy_contract_source,
    _normalize_legacy_transaction,
    _normalized_tx_view,
)
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime


class LegacyNetworkReplayTests(unittest.TestCase):
    def test_coerce_legacy_value_restores_runtime_scalars(self):
        self.assertEqual(_coerce_legacy_value("100"), 100)
        self.assertEqual(
            _coerce_legacy_value("1940.36"),
            ContractingDecimal("1940.36"),
        )
        self.assertTrue(_coerce_legacy_value("True"))
        self.assertFalse(_coerce_legacy_value("False"))
        self.assertIsNone(_coerce_legacy_value("None"))
        self.assertEqual(
            _coerce_legacy_value('{"ok": "True", "amount": "5"}'),
            {"ok": True, "amount": 5},
        )
        parsed_datetime = _coerce_legacy_value("2024-11-23T10:00:29.000000")
        self.assertIsInstance(parsed_datetime, Datetime)
        self.assertEqual(str(parsed_datetime), "2024-11-23 10:00:29")

    def test_normalize_legacy_transaction_maps_stamps_and_injects_artifacts(self):
        tx = _normalize_legacy_transaction(
            {
                "payload": {
                    "contract": "submission",
                    "function": "submit_contract",
                    "sender": "alice",
                    "nonce": "7",
                    "stamps_supplied": "42",
                    "kwargs": {
                        "name": "con_demo",
                        "code": "@export\ndef ping():\n    return 'pong'\n",
                    },
                },
                "metadata": {"signature": "deadbeef"},
            },
            block_meta={
                "height": 12,
                "hash": "BLOCK",
                "nanos": 123,
                "chain_id": "xian-1",
            },
            deployment_artifacts={"runtime_code": "compiled"},
        )

        self.assertEqual(tx["payload"]["chi_supplied"], 42)
        self.assertEqual(tx["payload"]["nonce"], 7)
        self.assertNotIn("stamps_supplied", tx["payload"])
        self.assertEqual(
            tx["payload"]["kwargs"]["deployment_artifacts"],
            {"runtime_code": "compiled"},
        )

    def test_normalized_tx_view_sorts_state_and_maps_stamps_used(self):
        view = _normalized_tx_view(
            {
                "status": 0,
                "result": "ok",
                "stamps_used": "9",
                "state": [
                    {"key": "z", "value": "True"},
                    {"key": "a", "value": "1.5"},
                ],
                "events": [
                    {
                        "event": "Transfer",
                        "data": {"amount": "1.5"},
                        "data_indexed": {"to": "bob"},
                    }
                ],
                "rewards": {"master": "3"},
            }
        )

        self.assertEqual(view["chi_used"], 9)
        self.assertEqual(view["state"][0]["key"], "a")
        self.assertEqual(view["state"][0]["value"], "1.5")
        self.assertEqual(view["state"][1]["value"], True)
        self.assertEqual(view["events"][0]["data"]["amount"], "1.5")
        self.assertEqual(view["rewards"]["master"], 3)

    def test_build_legacy_exported_state_restores_values(self):
        exported = _build_legacy_exported_state(
            [
                {"key": "currency.balances:alice", "value": "1.25"},
                {"key": "flag", "value": "True"},
            ]
        )

        self.assertEqual(exported["hash"], "0" * 64)
        self.assertEqual(
            exported["genesis"][0]["value"],
            ContractingDecimal("1.25"),
        )
        self.assertTrue(exported["genesis"][1]["value"])

    def test_build_runtime_code_inventory_extracts_contract_code(self):
        inventory = _build_runtime_code_inventory(
            [
                {"key": "currency.__code__", "value": "compiled"},
                {"key": "currency.__source__", "value": "source"},
                {"key": "con_demo.__code__", "value": "other"},
            ]
        )

        self.assertEqual(
            inventory,
            {"currency": "compiled", "con_demo": "other"},
        )

    def test_normalize_legacy_contract_source_upgrades_empty_logevent(self):
        source = """
TransferEvent = LogEvent()

@export
def ping(amount: float, to: str):
    TransferEvent({'from': ctx.caller, 'to': to, 'amount': amount})
"""
        runtime_code = """
__TransferEvent = LogEvent(
    event='Transfer',
    params={'from': {'type': str, 'idx': True}, 'to': {'type': str, 'idx': True}, 'amount': {'type': (int, float, decimal)}},
    contract='currency',
    name='TransferEvent',
)
"""

        normalized = _normalize_legacy_contract_source(
            contract_name="currency",
            source=source,
            runtime_code=runtime_code,
        )

        self.assertIn("TransferEvent = LogEvent('Transfer', {", normalized)
        self.assertIn("'amount': {'type': (int, float, decimal)}", normalized)

    def test_historical_record_from_graphql_node_uses_embedded_payload(self):
        record = _historical_record_from_graphql_node(
            {
                "hash": "abcd",
                "blockHeight": 12,
                "blockHash": "beef",
                "blockTime": "123",
                "jsonContent": {
                    "b_meta": {
                        "height": "12",
                        "hash": "beef",
                        "nanos": "123",
                        "chain_id": "xian-1",
                    },
                    "payload": {
                        "contract": "currency",
                        "function": "transfer",
                        "sender": "alice",
                        "nonce": "7",
                        "stamps_supplied": "9",
                        "kwargs": {"to": "bob", "amount": "1"},
                    },
                    "metadata": {"signature": "sig"},
                    "tx_result": {
                        "status": "0",
                        "result": "None",
                        "state": [],
                        "events": [],
                        "stamps_used": "9",
                    },
                },
            },
            tx_index=3,
        )

        self.assertEqual(record.tx_hash, "ABCD")
        self.assertEqual(record.height, 12)
        self.assertEqual(record.tx_index, 3)
        self.assertEqual(record.transaction["payload"]["chi_supplied"], 9)
        self.assertEqual(record.transaction["payload"]["kwargs"]["amount"], 1)
        self.assertEqual(record.historical_result["stamps_used"], "9")

    def test_state_patch_record_from_graphql_node_builds_non_replayable_record(self):
        record = _state_patch_record_from_graphql_node(
            {
                "hash": "STATE_PATCH_1",
                "blockHeight": 42,
                "blockHash": "beef",
                "blockTime": "123",
                "jsonContent": {"comment": "State Patch Pseudo-Transaction"},
                "stateChangesByTxHash": {
                    "nodes": [{"key": "currency.x", "value": "1"}]
                },
                "eventsByTxHash": {"nodes": []},
            },
            tx_index=0,
        )

        self.assertFalse(record.replayable)
        self.assertEqual(record.transaction["payload"]["contract"], "STATE_PATCHER")
        self.assertEqual(record.historical_result["state"][0]["key"], "currency.x")
        self.assertEqual(record.historical_result["status"], 0)

    def test_mismatch_fields_reports_changed_sections(self):
        expected = {
            "status": 0,
            "result": "ok",
            "state": [{"key": "a", "value": 1}],
            "events": [],
            "chi_used": 1,
            "rewards": None,
        }
        actual = {
            "status": 1,
            "result": "no",
            "state": [{"key": "a", "value": 2}],
            "events": [{"event": "Mismatch"}],
            "chi_used": 2,
            "rewards": {"x": 1},
        }

        self.assertEqual(
            _mismatch_fields(expected, actual),
            ["status", "result", "state", "events", "chi_used", "rewards"],
        )


if __name__ == "__main__":
    unittest.main()
