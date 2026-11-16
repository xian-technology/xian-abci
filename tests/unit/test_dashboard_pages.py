"""Dashboard query failures and bounded cursor pages stay visible to operators."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from xian.dashboard.app import _abci_query, handle_contracts, handle_validator_dashboard
from xian.dashboard.pages import bounded_page, namespace_cursor, numeric_cursor


class CursorBoundaryTests(unittest.TestCase):
    def test_numeric_cursors_are_optional_positive_integers(self):
        for raw, expected in ((None, None), (1, 1), ("2", 2)):
            self.assertEqual(numeric_cursor(raw), expected)
        for raw in (False, True, 0, -1, "0", "bad", {}, 1.5):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                numeric_cursor(raw)

    def test_namespace_cursors_have_an_inclusive_utf8_byte_bound(self):
        for raw in (None, "a" * 511, "é" * 255):
            self.assertEqual(namespace_cursor(raw), raw)
        for raw in (False, "", "a" * 512, "é" * 256):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                namespace_cursor(raw)

    def page(self, payload, **kwargs):
        return bounded_page(
            payload,
            items_key="items",
            next_key="next_cursor",
            limit=1,
            cursor=kwargs.pop("cursor", None),
            parse_cursor=numeric_cursor,
            **kwargs,
        )

    def test_complete_and_sparse_pages_keep_their_items_and_cursor(self):
        self.assertEqual(
            self.page({"items": [{"id": 1}], "next_cursor": None}), ([{"id": 1}], None)
        )
        self.assertEqual(self.page({"items": [], "next_cursor": 2}, cursor=1), ([], 2))
        self.assertEqual(
            self.page({"items": [], "next_cursor": 1}, cursor=2, descending=True), ([], 1)
        )
        self.assertEqual(self.page({"items": [], "next_cursor": None}, cursor=2), ([], None))
        self.assertEqual(self.page({"items": [], "next_cursor": 2}), ([], 2))

    def test_incomplete_or_oversized_envelopes_are_errors(self):
        for payload in (
            None,
            [],
            {"items": None},
            {"items": [1], "next_cursor": None},
            {"items": [{}, {}], "next_cursor": None},
            {"items": []},
        ):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                self.page(payload)
        for following, descending in ((2, False), (1, False), (2, True), (3, True)):
            with (
                self.subTest(following=following, descending=descending),
                self.assertRaisesRegex(RuntimeError, "cursor did not advance"),
            ):
                self.page({"items": [], "next_cursor": following}, cursor=2, descending=descending)


class DashboardPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_does_not_turn_a_failed_query_into_an_empty_list(self):
        upstream = {"response": {"code": 1, "log": "Use the bounded cursor route."}}
        with patch("xian.dashboard.app._raw_rpc", AsyncMock(return_value=upstream)):
            with self.assertRaisesRegex(RuntimeError, "Use the bounded cursor route"):
                await _abci_query(object(), "http://127.0.0.1:26657", "validators_open_votes")

    async def test_query_preserves_success_and_json_quotes_opaque_paths(self):
        query = AsyncMock(
            return_value={"response": {"code": 0, "value": base64.b64encode(b"[]").decode()}}
        )
        with patch("xian.dashboard.app._raw_rpc", query):
            self.assertEqual(await _abci_query(object(), "rpc", 'get/a"b\\c'), [])
        self.assertEqual(json.loads(query.await_args.args[3]["path"]), '/get/a"b\\c')
        with patch("xian.dashboard.app._raw_rpc", AsyncMock(return_value={"response": {}})):
            self.assertIsNone(await _abci_query(object(), "rpc", "get/missing"))
        with patch(
            "xian.dashboard.app._raw_rpc", AsyncMock(return_value={"response": {"code": 1}})
        ):
            with self.assertRaisesRegex(RuntimeError, "ABCI query failed"):
                await _abci_query(object(), "rpc", "get/missing")

    def request(self, **query):
        return SimpleNamespace(
            query=query, app={"session": object(), "rpc_url": "http://127.0.0.1:26657"}
        )

    async def test_contracts_preserve_empty_scan_page_continuation(self):
        page = {"items": [], "next_after": "currency", "limit": 50, "sort": "name", "order": "asc"}
        query = AsyncMock(return_value=page)
        with patch("xian.dashboard.app._abci_query", query):
            response = await handle_contracts(self.request(limit="50", after="con_alpha"))
        self.assertEqual(json.loads(response.text), page)
        self.assertEqual(query.await_count, 1)
        self.assertEqual(query.await_args.args[2], "contracts_page/limit=50/after=con_alpha")

    async def test_contracts_reject_a_repeated_or_backwards_cursor(self):
        for following in ("con_alpha", "con_a"):
            with self.subTest(following=following):
                page = {"items": [], "next_after": following}
                with patch("xian.dashboard.app._abci_query", AsyncMock(return_value=page)):
                    response = await handle_contracts(self.request(after="con_alpha"))
                self.assertEqual(response.status, 500)
                self.assertIn("cursor did not advance", json.loads(response.text)["error"])

    async def test_contract_request_caps_and_refusals_are_explicit(self):
        for raw, expected in (("500", 100), ("0", 1)):
            query = AsyncMock(return_value={"items": [], "next_after": None})
            with patch("xian.dashboard.app._abci_query", query):
                response = await handle_contracts(self.request(limit=raw))
            self.assertEqual(json.loads(response.text)["limit"], expected)
            self.assertEqual(query.await_args.args[2], f"contracts_page/limit={expected}")
        for query_params in ({"offset": "1"}, {"after": ""}, {"after": "a" * 512}):
            with self.subTest(query_params=query_params), self.assertRaises(web.HTTPBadRequest):
                await handle_contracts(self.request(**query_params))

    async def test_bad_validator_cursors_do_not_start_queries(self):
        query = AsyncMock()
        with patch("xian.dashboard.app._abci_query", query):
            for params in ({"open_votes_cursor": "0"}, {"pending_unbonds_cursor": "bad"}):
                with self.subTest(params=params), self.assertRaises(web.HTTPBadRequest):
                    await handle_validator_dashboard(self.request(**params))
        query.assert_not_awaited()

    async def test_contract_transport_failure_is_unavailable_not_empty(self):
        with patch(
            "xian.dashboard.app._abci_query", AsyncMock(side_effect=aiohttp.ClientError("offline"))
        ):
            response = await handle_contracts(self.request())
        self.assertEqual(response.status, 502)
        self.assertIn("offline", json.loads(response.text)["error"])

    async def test_validator_summary_uses_bounded_pages_and_exposes_continuation(self):
        account = "a" * 64
        responses = {
            "validators_policy": {},
            "validators_active": [],
            "validators_candidates": [],
            "validators_open_votes_page/limit=25/cursor=10": {
                "items": [],
                "next_cursor": 5,
                "limit": 25,
            },
            f"validators_validator/{account}": {"account": account},
            f"validators_pending_unbonds_page/{account}/limit=100/cursor=20": {
                "unbonds": [{"unbond_id": 20}],
                "next_cursor": 120,
            },
        }
        status = {
            "validator_info": {
                "pub_key": {"value": base64.b64encode(bytes.fromhex(account)).decode()}
            }
        }
        query = AsyncMock(side_effect=lambda _s, _r, path: responses[path])
        with (
            patch("xian.dashboard.app._raw_rpc", AsyncMock(return_value=status)),
            patch("xian.dashboard.app._abci_query", query),
        ):
            response = await handle_validator_dashboard(
                self.request(open_votes_cursor="10", pending_unbonds_cursor="20")
            )
        payload = json.loads(response.text)
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["open_votes"], [])
        self.assertEqual(payload["open_votes_next_cursor"], 5)
        self.assertEqual(payload["local_pending_unbonds"], [{"unbond_id": 20}])
        self.assertEqual(payload["local_pending_unbonds_next_cursor"], 120)
