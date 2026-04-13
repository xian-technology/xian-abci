from __future__ import annotations

import ast
import base64
import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from contracting.client import ContractingClient
from contracting.compilation.artifacts import build_contract_artifacts
from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

from xian.execution_engine import build_execution_runtime, prepare_contract_for_execution
from xian.execution_policy import ExecutionPolicy
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.services.bds.reindex import (
    CometBftRpcClient,
    datetime_to_nanos,
    parse_rfc3339_nano,
)
from xian.state_export import import_state
from xian.utils.encoding import (
    decode_transaction_bytes,
    hash_bytes,
    normalize_for_abci_json,
    stringify_decimals,
)

_INT_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
_EXPONENTIAL_RE = re.compile(r"^-?\d+(?:\.\d+)?[eE][+-]?\d+$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)

_CONTRACTS_QUERY = """
query($first: Int!, $offset: Int!) {
  allContracts(first: $first, offset: $offset) {
    nodes {
      name
      txHash
      created
      code
    }
  }
}
""".strip()

_GENESIS_QUERY = """
query {
  allTransactions(first: 1, condition: {hash: "GENESIS"}) {
    nodes {
      hash
      blockHeight
      jsonContent
    }
  }
}
""".strip()

_FIRST_REAL_TX_QUERY = """
query {
  allTransactions(first: 1, orderBy: BLOCK_HEIGHT_ASC, offset: 1) {
    nodes {
      blockHeight
    }
  }
}
""".strip()

_TRANSACTION_INVENTORY_QUERY = """
query($first: Int!, $offset: Int!) {
  allTransactions(first: $first, offset: $offset, orderBy: BLOCK_HEIGHT_ASC) {
    nodes {
      hash
      blockHeight
      success
      contract
      function
    }
  }
}
""".strip()

_TRANSACTION_REPLAY_QUERY = """
query($first: Int!, $offset: Int!) {
  allTransactions(first: $first, offset: $offset, orderBy: CREATED_ASC) {
    nodes {
      hash
      blockHeight
      blockHash
      blockTime
      success
      contract
      function
      jsonContent
    }
  }
}
""".strip()

_STATE_PATCH_QUERY = """
query($hash: String!) {
  allTransactions(first: 1, condition: {hash: $hash}) {
    nodes {
      hash
      blockHeight
      blockHash
      blockTime
      contract
      function
      jsonContent
      stateChangesByTxHash {
        nodes {
          key
          value
        }
      }
      eventsByTxHash {
        nodes {
          event
          caller
          signer
          contract
          data
          dataIndexed
        }
      }
    }
  }
}
""".strip()


@dataclass(slots=True, frozen=True)
class LegacyContractRecord:
    name: str
    tx_hash: str
    created: str | None
    source: str | None


@dataclass(slots=True)
class ContractAuditResult:
    name: str
    tx_hash: str
    compatible: bool
    reason: str | None = None
    source: str | None = None
    artifacts: dict[str, Any] | None = None

    def as_report_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tx_hash": self.tx_hash,
            "compatible": self.compatible,
            "reason": self.reason,
        }


@dataclass(slots=True, frozen=True)
class HistoricalTxRecord:
    tx_hash: str
    height: int
    tx_index: int
    transaction: dict[str, Any]
    historical_result: dict[str, Any]
    replayable: bool = True


@dataclass(slots=True, frozen=True)
class LegacyTransactionInventoryRecord:
    tx_hash: str
    block_height: int
    success: bool
    contract: str
    function: str


def _legacy_runtime_event_schemas(runtime_code: str | None) -> dict[str, tuple[str, ast.AST]]:
    if not runtime_code:
        return {}
    try:
        tree = ast.parse(runtime_code)
    except SyntaxError:
        return {}

    schemas: dict[str, tuple[str, ast.AST]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        call = statement.value
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Name) or call.func.id != "LogEvent":
            continue
        keyword_values = {keyword.arg: keyword.value for keyword in call.keywords}
        event_node = keyword_values.get("event")
        params_node = keyword_values.get("params")
        if not isinstance(event_node, ast.Constant) or not isinstance(
            event_node.value, str
        ):
            continue
        if params_node is None:
            continue
        public_name = target.id.lstrip("_")
        schemas[public_name] = (event_node.value, copy.deepcopy(params_node))
    return schemas


class _LegacyLogEventNormalizer(ast.NodeTransformer):
    def __init__(self, schemas: dict[str, tuple[str, ast.AST]]) -> None:
        self.schemas = schemas
        self.changed = False

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node = self.generic_visit(node)
        if len(node.targets) != 1:
            return node
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            return node
        value = node.value
        if not isinstance(value, ast.Call):
            return node
        if not isinstance(value.func, ast.Name) or value.func.id != "LogEvent":
            return node
        if value.args or value.keywords:
            return node
        schema = self.schemas.get(target.id)
        if schema is None:
            return node
        event_name, params_node = schema
        node.value = ast.copy_location(
            ast.Call(
                func=ast.Name(id="LogEvent", ctx=ast.Load()),
                args=[ast.Constant(event_name), copy.deepcopy(params_node)],
                keywords=[],
            ),
            value,
        )
        self.changed = True
        return node


def _normalize_legacy_contract_source(
    *,
    contract_name: str,
    source: str | None,
    runtime_code: str | None,
) -> str | None:
    if source is None:
        return None

    schemas = _legacy_runtime_event_schemas(runtime_code)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    normalizer = _LegacyLogEventNormalizer(schemas)
    normalized = normalizer.visit(tree)
    ast.fix_missing_locations(normalized)
    rendered = ast.unparse(normalized)
    if source.endswith("\n"):
        rendered += "\n"
    return rendered


def _json_safe(value: Any) -> Any:
    return stringify_decimals(normalize_for_abci_json(value))


def _coerce_legacy_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_coerce_legacy_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _coerce_legacy_value(item)
            for key, item in value.items()
        }
    if not isinstance(value, str):
        return value

    if value == "True":
        return True
    if value == "False":
        return False
    if value == "None":
        return None
    if _INT_RE.fullmatch(value):
        return int(value)
    if _DECIMAL_RE.fullmatch(value) or _EXPONENTIAL_RE.fullmatch(value):
        return ContractingDecimal(value)
    if _ISO_DATETIME_RE.fullmatch(value):
        resolved = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(resolved)
        return Datetime._from_datetime(parsed.replace(tzinfo=None))

    if value and value[0] in {'"', "[", "{", "n"}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return _coerce_legacy_value(parsed)

    return value


def _normalize_state_entries(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not entries:
        return []
    normalized = []
    for entry in entries:
        normalized.append(
            {
                "key": str(entry["key"]),
                "value": _json_safe(_coerce_legacy_value(entry.get("value"))),
            }
        )
    normalized.sort(key=lambda item: item["key"])
    return normalized


def _normalized_tx_view(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    chi_used = result.get("chi_used", result.get("stamps_used", 0))
    rewards = result.get("rewards")
    return {
        "status": int(result.get("status", 0)),
        "result": _json_safe(result.get("result")),
        "state": _normalize_state_entries(result.get("state")),
        "events": _json_safe(_coerce_legacy_value(result.get("events", []))),
        "chi_used": int(chi_used or 0),
        "rewards": _json_safe(_coerce_legacy_value(rewards)),
    }


def _historical_tx_succeeded(result: dict[str, Any] | None) -> bool:
    if result is None:
        return False
    return int(result.get("status", 0)) == 0


def _mismatch_fields(
    expected: dict[str, Any] | None,
    actual: dict[str, Any] | None,
) -> list[str]:
    if expected is None and actual is None:
        return []
    if expected is None or actual is None:
        return ["tx_result"]
    mismatches: list[str] = []
    for field in ("status", "result", "state", "events", "chi_used", "rewards"):
        if expected.get(field) != actual.get(field):
            mismatches.append(field)
    return mismatches


def _subset_mismatch_fields(
    expected: dict[str, Any] | None,
    actual: dict[str, Any] | None,
    *,
    fields: tuple[str, ...],
) -> list[str]:
    if expected is None and actual is None:
        return []
    if expected is None or actual is None:
        return ["tx_result"]
    mismatches: list[str] = []
    for field in fields:
        if expected.get(field) != actual.get(field):
            mismatches.append(field)
    return mismatches


def _decode_legacy_tx_result(tx_result_rpc: dict[str, Any]) -> dict[str, Any] | None:
    data_b64 = tx_result_rpc.get("data")
    if not data_b64:
        return None
    decoded = base64.b64decode(data_b64).decode("utf-8")
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        return None
    if "chi_used" not in parsed and "stamps_used" in parsed:
        parsed["chi_used"] = parsed["stamps_used"]
    parsed.setdefault("state", [])
    parsed.setdefault("events", [])
    parsed.setdefault("rewards", None)
    parsed.setdefault("status", int(tx_result_rpc.get("code", 0)))
    return parsed


def _normalize_legacy_transaction(
    envelope: dict[str, Any],
    *,
    block_meta: dict[str, Any],
    deployment_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(envelope["payload"])
    if "chi_supplied" not in payload:
        payload["chi_supplied"] = int(payload.get("stamps_supplied", 0))
    payload["chi_supplied"] = int(payload.get("chi_supplied", 0))
    if "nonce" in payload:
        payload["nonce"] = int(payload["nonce"])
    payload.pop("stamps_supplied", None)
    if deployment_artifacts is not None:
        kwargs = dict(payload.get("kwargs") or {})
        kwargs["deployment_artifacts"] = deployment_artifacts
        payload["kwargs"] = kwargs
    return {
        "payload": payload,
        "metadata": dict(envelope.get("metadata") or {}),
        "b_meta": dict(block_meta),
    }


def _build_legacy_exported_state(genesis_entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "hash": "0" * 64,
        "number": 0,
        "nanos": 0,
        "origin": {"signature": "", "sender": ""},
        "genesis": [
            {
                "key": str(entry["key"]),
                "value": _coerce_legacy_value(entry.get("value")),
            }
            for entry in genesis_entries
        ],
        "nonces": [],
    }


def _build_runtime_code_inventory(
    genesis_entries: list[dict[str, Any]],
) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for entry in genesis_entries:
        key = str(entry["key"])
        if not key.endswith(".__code__"):
            continue
        value = entry.get("value")
        if isinstance(value, str):
            inventory[key[: -len(".__code__")]] = value
    return inventory


def _coerce_graphql_tx_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("legacy GraphQL transaction jsonContent must be a dict")
    payload = dict(value)
    tx_payload = dict(payload.get("payload") or {})
    if "kwargs" in tx_payload:
        tx_payload["kwargs"] = _coerce_legacy_value(tx_payload.get("kwargs"))
    payload["payload"] = tx_payload
    return payload


def _historical_record_from_graphql_node(
    node: dict[str, Any],
    *,
    tx_index: int,
) -> HistoricalTxRecord:
    tx_hash = str(node["hash"]).upper()
    block_height = int(node["blockHeight"])
    payload = _coerce_graphql_tx_payload(node["jsonContent"])
    block_meta = payload.get("b_meta")
    if not isinstance(block_meta, dict):
        block_meta = {
            "height": block_height,
            "hash": str(node.get("blockHash") or "").upper(),
            "nanos": int(node.get("blockTime") or 0),
            "chain_id": str(
                ((payload.get("payload") or {}).get("chain_id")) or "xian-1"
            ),
        }
    else:
        block_meta = dict(block_meta)
        block_meta["height"] = int(block_meta.get("height", block_height))
        block_meta["hash"] = str(
            block_meta.get("hash", node.get("blockHash") or "")
        ).upper()
        block_meta["nanos"] = int(block_meta.get("nanos", node.get("blockTime") or 0))
        block_meta["chain_id"] = str(
            block_meta.get(
                "chain_id",
                ((payload.get("payload") or {}).get("chain_id")) or "xian-1",
            )
        )
    envelope = {
        "payload": payload.get("payload") or {},
        "metadata": payload.get("metadata") or {},
    }
    normalized_tx = _normalize_legacy_transaction(
        envelope,
        block_meta=block_meta,
    )
    historical_result = payload.get("tx_result")
    if not isinstance(historical_result, dict):
        raise ValueError(f"legacy tx {tx_hash} missing tx_result payload")
    return HistoricalTxRecord(
        tx_hash=tx_hash,
        height=block_height,
        tx_index=tx_index,
        transaction=normalized_tx,
        historical_result=historical_result,
    )


def _state_patch_record_from_graphql_node(
    node: dict[str, Any],
    *,
    tx_index: int,
) -> HistoricalTxRecord:
    tx_hash = str(node["hash"]).upper()
    block_height = int(node["blockHeight"])
    block_meta = {
        "height": block_height,
        "hash": str(node.get("blockHash") or "").upper(),
        "nanos": int(node.get("blockTime") or 0),
        "chain_id": "xian-1",
    }
    state_changes = (
        ((node.get("stateChangesByTxHash") or {}).get("nodes")) or []
    )
    events = ((node.get("eventsByTxHash") or {}).get("nodes")) or []
    return HistoricalTxRecord(
        tx_hash=tx_hash,
        height=block_height,
        tx_index=tx_index,
        transaction={
            "payload": {
                "contract": "STATE_PATCHER",
                "function": "STATE_PATCH",
                "sender": "STATE_PATCHER",
                "nonce": 0,
                "chi_supplied": 0,
                "kwargs": {},
            },
            "metadata": {"signature": ""},
            "b_meta": block_meta,
        },
        historical_result={
            "status": 0,
            "result": (node.get("jsonContent") or {}).get(
                "comment",
                "State Patch Pseudo-Transaction",
            ),
            "state": state_changes,
            "events": events,
            "chi_used": 0,
            "rewards": None,
        },
        replayable=False,
    )


def _apply_historical_state_changes(driver, state_changes: list[dict[str, Any]]) -> None:
    writes = {
        str(entry["key"]): _coerce_legacy_value(entry.get("value"))
        for entry in state_changes
    }
    driver.apply_writes(writes)


def _extract_contract_sources_from_state_changes(
    state_changes: list[dict[str, Any]],
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for entry in state_changes:
        key = str(entry.get("key"))
        if not key.endswith(".__source__"):
            continue
        value = entry.get("value")
        if isinstance(value, str):
            sources[key[: -len(".__source__")]] = value
    return sources


def _build_artifacts_for_source(
    *,
    module_name: str,
    source: str,
) -> dict[str, Any]:
    return build_contract_artifacts(
        module_name=module_name,
        source=source,
        lint=True,
        vm_profile="xian_vm_v1",
    )


def _install_contract_artifacts(
    driver,
    *,
    name: str,
    artifacts: dict[str, Any],
) -> None:
    owner = driver.get_owner(name)
    timestamp = driver.get_time_submitted(name)
    developer = driver.get_contract_developer(name)
    deployer = driver.get_contract_deployer(name)
    initiator = driver.get_contract_initiator(name)
    driver.set_contract(
        name=name,
        code=artifacts["runtime_code"],
        source=artifacts["source"],
        vm_ir_json=artifacts["vm_ir_json"],
        owner=owner,
        overwrite=True,
        timestamp=timestamp,
        developer=developer,
        deployer=deployer,
        initiator=initiator,
    )


def _install_builtin_submission(client: ContractingClient) -> None:
    runtime_code, source_code, vm_ir_json = client._load_submission_artifacts(
        client.submission_filename
    )
    client.raw_driver.set_contract(
        name="submission",
        code=runtime_code,
        source=source_code,
        vm_ir_json=vm_ir_json,
        owner=client.raw_driver.get_owner("submission"),
        overwrite=True,
        timestamp=client.raw_driver.get_time_submitted("submission"),
        developer=client.raw_driver.get_contract_developer("submission"),
        deployer=client.raw_driver.get_contract_deployer("submission"),
        initiator=client.raw_driver.get_contract_initiator("submission"),
    )
    client.raw_driver.commit()


class LegacyNetworkGraphqlClient:
    def __init__(self, graphql_url: str):
        self.graphql_url = graphql_url
        self._session: aiohttp.ClientSession | None = None

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = await self._session_or_create()
        async with session.post(
            self.graphql_url,
            json={"query": query, "variables": variables or {}},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("errors"):
            raise ValueError(f"GraphQL query failed: {payload['errors']}")
        return payload["data"]

    async def fetch_genesis_state(self) -> list[dict[str, Any]]:
        data = await self.query(_GENESIS_QUERY)
        nodes = data["allTransactions"]["nodes"]
        if not nodes:
            raise ValueError("GENESIS transaction not found in legacy GraphQL")
        genesis = nodes[0]["jsonContent"]
        if not isinstance(genesis, list):
            raise ValueError("GENESIS jsonContent must be a key/value list")
        return genesis

    async def fetch_first_real_tx_height(self) -> int:
        data = await self.query(_FIRST_REAL_TX_QUERY)
        nodes = data["allTransactions"]["nodes"]
        if not nodes:
            raise ValueError("No historical transactions found in GraphQL")
        return int(nodes[0]["blockHeight"])

    async def fetch_contract_inventory(
        self,
        *,
        page_size: int = 100,
    ) -> list[LegacyContractRecord]:
        records: list[LegacyContractRecord] = []
        offset = 0
        while True:
            data = await self.query(
                _CONTRACTS_QUERY,
                {"first": page_size, "offset": offset},
            )
            nodes = data["allContracts"]["nodes"]
            if not nodes:
                break
            for node in nodes:
                records.append(
                    LegacyContractRecord(
                        name=str(node["name"]),
                        tx_hash=str(node["txHash"]),
                        created=node.get("created"),
                        source=node.get("code"),
                    )
                )
            if len(nodes) < page_size:
                break
            offset += page_size
        return records

    async def fetch_transaction_inventory(
        self,
        *,
        page_size: int = 500,
    ) -> list[LegacyTransactionInventoryRecord]:
        records: list[LegacyTransactionInventoryRecord] = []
        offset = 0
        while True:
            data = await self.query(
                _TRANSACTION_INVENTORY_QUERY,
                {"first": page_size, "offset": offset},
            )
            nodes = data["allTransactions"]["nodes"]
            if not nodes:
                break
            for node in nodes:
                block_height = int(node["blockHeight"])
                if block_height <= 0:
                    continue
                records.append(
                    LegacyTransactionInventoryRecord(
                        tx_hash=str(node["hash"]),
                        block_height=block_height,
                        success=bool(node["success"]),
                        contract=str(node["contract"]),
                        function=str(node["function"]),
                    )
                )
            if len(nodes) < page_size:
                break
            offset += page_size
        return records

    async def fetch_state_patch_record(
        self,
        tx_hash: str,
    ) -> dict[str, Any] | None:
        data = await self.query(_STATE_PATCH_QUERY, {"hash": tx_hash})
        nodes = data["allTransactions"]["nodes"]
        if not nodes:
            return None
        return nodes[0]

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


async def audit_contract_inventory(
    contracts: list[LegacyContractRecord],
    *,
    runtime_code_inventory: dict[str, str] | None = None,
) -> tuple[dict[str, ContractAuditResult], list[dict[str, Any]]]:
    audit: dict[str, ContractAuditResult] = {}
    incompatible: list[dict[str, Any]] = []
    runtime_code_inventory = runtime_code_inventory or {}
    for contract in contracts:
        if not contract.source:
            result = ContractAuditResult(
                name=contract.name,
                tx_hash=contract.tx_hash,
                compatible=False,
                reason="missing source in contract inventory",
                source=None,
            )
            audit[contract.name] = result
            incompatible.append(result.as_report_dict())
            continue
        try:
            normalized_source = _normalize_legacy_contract_source(
                contract_name=contract.name,
                source=contract.source,
                runtime_code=runtime_code_inventory.get(contract.name),
            )
            artifacts = _build_artifacts_for_source(
                module_name=contract.name,
                source=normalized_source or contract.source,
            )
            audit[contract.name] = ContractAuditResult(
                name=contract.name,
                tx_hash=contract.tx_hash,
                compatible=True,
                source=normalized_source or contract.source,
                artifacts=artifacts,
            )
        except Exception as exc:
            result = ContractAuditResult(
                name=contract.name,
                tx_hash=contract.tx_hash,
                compatible=False,
                reason=str(exc),
                source=contract.source,
            )
            audit[contract.name] = result
            incompatible.append(result.as_report_dict())
    incompatible.sort(key=lambda item: item["name"])
    return audit, incompatible


async def iter_historical_transactions(
    rpc_client: CometBftRpcClient,
    *,
    heights: list[int],
    max_transactions: int | None = None,
):
    yielded = 0
    for height in heights:
        block_response = await rpc_client.block(height)
        block_results = await rpc_client.block_results(height)
        block = block_response["block"]
        header = block["header"]
        block_time = parse_rfc3339_nano(header["time"])
        block_meta = {
            "height": int(header["height"]),
            "hash": str(block_response["block_id"]["hash"]).upper(),
            "nanos": datetime_to_nanos(block_time),
            "chain_id": str(header["chain_id"]),
        }
        txs = list((block.get("data") or {}).get("txs") or [])
        results = list(block_results.get("txs_results") or [])
        if len(txs) != len(results):
            raise ValueError(
                f"block {height} tx/result count mismatch: "
                f"{len(txs)} txs vs {len(results)} results"
            )
        for tx_index, (tx_b64, tx_result_rpc) in enumerate(
            zip(txs, results, strict=True)
        ):
            raw_tx = base64.b64decode(tx_b64)
            envelope, _ = decode_transaction_bytes(raw_tx)
            tx_hash = hash_bytes(raw_tx).upper()
            historical_result = _decode_legacy_tx_result(tx_result_rpc)
            if historical_result is None:
                continue
            yield HistoricalTxRecord(
                tx_hash=tx_hash,
                height=height,
                tx_index=tx_index,
                transaction=_normalize_legacy_transaction(
                    envelope,
                    block_meta=block_meta,
                ),
                historical_result=historical_result,
            )
            yielded += 1
            if max_transactions is not None and yielded >= max_transactions:
                return


async def iter_historical_transactions_graphql(
    graphql_client: LegacyNetworkGraphqlClient,
    *,
    start_height: int,
    end_height: int,
    max_transactions: int | None = None,
    page_size: int = 250,
):
    yielded = 0
    offset = 1  # skip GENESIS pseudo-transaction
    tx_index_by_height: dict[int, int] = {}
    while True:
        data = await graphql_client.query(
            _TRANSACTION_REPLAY_QUERY,
            {"first": page_size, "offset": offset},
        )
        nodes = data["allTransactions"]["nodes"]
        if not nodes:
            return
        for node in nodes:
            block_height = int(node["blockHeight"])
            if block_height <= 0:
                continue
            if block_height < start_height:
                continue
            if block_height > end_height:
                return
            tx_index = tx_index_by_height.get(block_height, 0)
            try:
                record = _historical_record_from_graphql_node(
                    node,
                    tx_index=tx_index,
                )
            except ValueError:
                tx_hash = str(node["hash"]).upper()
                if not tx_hash.startswith("STATE_PATCH_"):
                    continue
                patch_node = await graphql_client.fetch_state_patch_record(tx_hash)
                if patch_node is None:
                    continue
                record = _state_patch_record_from_graphql_node(
                    patch_node,
                    tx_index=tx_index,
                )
            tx_index_by_height[block_height] = tx_index + 1
            yield record
            yielded += 1
            if max_transactions is not None and yielded >= max_transactions:
                return
        offset += len(nodes)


class LegacyReplayHarness:
    def __init__(self, *, storage_root: Path):
        self.storage_root = storage_root
        self.python_home = storage_root / "python"
        self.native_home = storage_root / "native"
        self.python_home.mkdir(parents=True, exist_ok=True)
        self.native_home.mkdir(parents=True, exist_ok=True)

        self.python_client = ContractingClient(storage_home=self.python_home)
        self.native_client = ContractingClient(storage_home=self.native_home)
        self.python_processor = TxProcessor(client=self.python_client)
        self.native_processor = TxProcessor(
            client=self.native_client,
            execution_runtime=build_execution_runtime(
                ExecutionPolicy(
                    mode="xian_vm_v1",
                    bytecode_version="xvm-1",
                    gas_schedule="xvm-gas-1",
                    authority="native",
                )
            ),
        )
        self.python_rewards = RewardsHandler(client=self.python_client)
        self.native_rewards = RewardsHandler(client=self.native_client)
        self._python_contract_exists: dict[str, bool] = {}
        self._native_contract_readiness: dict[str, tuple[bool, str | None]] = {}

    def close(self) -> None:
        self.python_client.flush()
        self.native_client.flush()

    def seed_genesis(
        self,
        genesis_entries: list[dict[str, Any]],
    ) -> None:
        exported_state = _build_legacy_exported_state(genesis_entries)
        import_state(
            exported_state=exported_state,
            storage_home=self.python_home,
        )
        import_state(
            exported_state=exported_state,
            storage_home=self.native_home,
        )
        _install_builtin_submission(self.python_client)
        _install_builtin_submission(self.native_client)

    def install_genesis_contracts(
        self,
        audit: dict[str, ContractAuditResult],
    ) -> int:
        installs = 0
        for result in audit.values():
            if not result.compatible:
                continue
            if (
                result.tx_hash != "GENESIS"
                and self.python_client.raw_driver.get_contract_source(result.name)
                is None
            ):
                continue
            assert result.artifacts is not None
            _install_contract_artifacts(
                self.python_client.raw_driver,
                name=result.name,
                artifacts=result.artifacts,
            )
            _install_contract_artifacts(
                self.native_client.raw_driver,
                name=result.name,
                artifacts=result.artifacts,
            )
            self._python_contract_exists[result.name] = True
            self._native_contract_readiness[result.name] = (True, None)
            installs += 1
        if installs:
            self.python_client.raw_driver.commit()
            self.native_client.raw_driver.commit()
        return installs

    def _prepare_deployment_artifacts(
        self,
        tx: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        payload = tx["payload"]
        if (
            payload["contract"] != "submission"
            or payload["function"] != "submit_contract"
        ):
            return None, None
        kwargs = dict(payload.get("kwargs") or {})
        name = kwargs.get("name")
        source = kwargs.get("code")
        if not isinstance(name, str) or not isinstance(source, str):
            return None, "submission tx missing deployable source payload"
        try:
            artifacts = _build_artifacts_for_source(
                module_name=name,
                source=source,
            )
        except Exception as exc:
            return None, str(exc)
        return artifacts, None

    def _can_run_python(self, tx: dict[str, Any]) -> bool:
        contract_name = tx["payload"]["contract"]
        cached = self._python_contract_exists.get(contract_name)
        if cached is not None:
            return cached
        exists = (
            self.python_client.raw_driver.get_contract(contract_name) is not None
        )
        self._python_contract_exists[contract_name] = exists
        return exists

    def _can_run_native(self, tx: dict[str, Any]) -> tuple[bool, str | None]:
        contract_name = tx["payload"]["contract"]
        cached = self._native_contract_readiness.get(contract_name)
        if cached is not None:
            return cached
        try:
            prepare_contract_for_execution(
                self.native_processor.execution_runtime,
                self.native_client.raw_driver,
                contract_name,
            )
        except Exception as exc:
            resolved = (False, str(exc))
            self._native_contract_readiness[contract_name] = resolved
            return resolved
        resolved = (True, None)
        self._native_contract_readiness[contract_name] = resolved
        return resolved

    def _rollback_pending(self) -> None:
        self.python_client.raw_driver.rollback()
        self.native_client.raw_driver.rollback()

    def _advance_historical_state(
        self,
        *,
        tx: dict[str, Any],
        historical_result: dict[str, Any],
        deployment_artifacts: dict[str, Any] | None,
    ) -> tuple[int, set[str]]:
        nanos = int(tx["b_meta"]["nanos"])
        installed_names: set[str] = set()
        install_count = 0
        for client in (self.python_client, self.native_client):
            _apply_historical_state_changes(
                client.raw_driver,
                historical_result.get("state") or [],
            )
        if deployment_artifacts is not None:
            name = tx["payload"]["kwargs"]["name"]
            try:
                _install_contract_artifacts(
                    self.python_client.raw_driver,
                    name=name,
                    artifacts=deployment_artifacts,
                )
                _install_contract_artifacts(
                    self.native_client.raw_driver,
                    name=name,
                    artifacts=deployment_artifacts,
                )
                installed_names.add(name)
                install_count += 1
            except Exception:
                pass
        for name, source in _extract_contract_sources_from_state_changes(
            historical_result.get("state") or []
        ).items():
            try:
                artifacts = _build_artifacts_for_source(
                    module_name=name,
                    source=source,
                )
            except Exception:
                continue
            try:
                _install_contract_artifacts(
                    self.python_client.raw_driver,
                    name=name,
                    artifacts=artifacts,
                )
                _install_contract_artifacts(
                    self.native_client.raw_driver,
                    name=name,
                    artifacts=artifacts,
                )
            except Exception:
                continue
            installed_names.add(name)
            install_count += 1
        self.python_client.raw_driver.hard_apply(nanos)
        self.native_client.raw_driver.hard_apply(nanos)
        return install_count, installed_names

    def replay_record(
        self,
        record: HistoricalTxRecord,
        *,
        logic_only: bool = False,
        native_only: bool = False,
    ) -> dict[str, Any]:
        tx = record.transaction
        if not record.replayable:
            self._rollback_pending()
            artifact_install_count, installed_names = self._advance_historical_state(
                tx=tx,
                historical_result=record.historical_result,
                deployment_artifacts=None,
            )
            for name in installed_names:
                self._python_contract_exists[name] = True
                self._native_contract_readiness[name] = (True, None)
            return {
                "tx_hash": record.tx_hash,
                "height": record.height,
                "tx_index": record.tx_index,
                "contract": tx["payload"]["contract"],
                "function": tx["payload"]["function"],
                "python_ran": False,
                "native_ran": False,
                "python_logic_ran": False,
                "native_logic_ran": False,
                "python_strict_vs_historical": [],
                "native_strict_vs_historical": [],
                "python_logic_vs_historical": [],
                "native_logic_vs_historical": [],
                "python_strict_vs_native": [],
                "python_logic_vs_native": [],
                "python_error": None,
                "native_error": None,
                "skip_reason": None,
                "deployment_artifacts_injected": False,
                "deployment_artifacts_error": None,
                "artifact_install_count": artifact_install_count,
                "historical": _normalized_tx_view(record.historical_result),
                "python_strict": None,
                "native_strict": None,
                "python_logic": None,
                "native_logic": None,
                "pseudo_state_patch": True,
            }
        deployment_artifacts, deployment_error = self._prepare_deployment_artifacts(
            tx
        )
        normalized_tx = _normalize_legacy_transaction(
            tx,
            block_meta=tx["b_meta"],
            deployment_artifacts=deployment_artifacts,
        )
        historical_view = _normalized_tx_view(record.historical_result)

        python_strict_view = None
        native_strict_view = None
        python_logic_view = None
        native_logic_view = None
        python_strict_mismatches: list[str] = []
        native_strict_mismatches: list[str] = []
        python_native_strict_mismatches: list[str] = []
        python_logic_mismatches: list[str] = []
        native_logic_mismatches: list[str] = []
        python_native_logic_mismatches: list[str] = []
        python_error: str | None = None
        native_error: str | None = None
        skipped_reason: str | None = None
        artifact_install_count = 0

        should_run_python = not native_only
        should_run_strict = not logic_only

        if should_run_python and self._can_run_python(normalized_tx):
            try:
                if should_run_strict:
                    output = self.python_processor.process_tx(
                        tx=normalized_tx,
                        enabled_fees=True,
                        rewards_handler=self.python_rewards,
                    )
                    python_strict_view = _normalized_tx_view(output["tx_result"])
                    python_strict_mismatches = _mismatch_fields(
                        historical_view,
                        python_strict_view,
                    )
                    self.python_client.raw_driver.rollback()
                output = self.python_processor.process_tx(
                    tx=normalized_tx,
                    enabled_fees=False,
                )
                python_logic_view = _normalized_tx_view(output["tx_result"])
                python_logic_mismatches = _subset_mismatch_fields(
                    historical_view,
                    python_logic_view,
                    fields=("status", "result", "events"),
                )
            except Exception as exc:  # pragma: no cover - defensive
                python_error = str(exc)
        elif should_run_python:
            skipped_reason = (
                f"python contract {normalized_tx['payload']['contract']} not installed"
            )

        native_can_run, native_block_reason = self._can_run_native(normalized_tx)
        if native_can_run:
            try:
                if should_run_strict:
                    output = self.native_processor.process_tx(
                        tx=normalized_tx,
                        enabled_fees=True,
                        rewards_handler=self.native_rewards,
                    )
                    native_strict_view = _normalized_tx_view(output["tx_result"])
                    native_strict_mismatches = _mismatch_fields(
                        historical_view,
                        native_strict_view,
                    )
                    self.native_client.raw_driver.rollback()
                output = self.native_processor.process_tx(
                    tx=normalized_tx,
                    enabled_fees=False,
                )
                native_logic_view = _normalized_tx_view(output["tx_result"])
                native_logic_mismatches = _subset_mismatch_fields(
                    historical_view,
                    native_logic_view,
                    fields=("status", "result", "events"),
                )
            except Exception as exc:  # pragma: no cover - defensive
                native_error = str(exc)
        else:
            native_error = native_block_reason
            if skipped_reason is None:
                skipped_reason = native_block_reason

        if python_strict_view is not None and native_strict_view is not None:
            python_native_strict_mismatches = _mismatch_fields(
                python_strict_view,
                native_strict_view,
            )
        if python_logic_view is not None and native_logic_view is not None:
            python_native_logic_mismatches = _subset_mismatch_fields(
                python_logic_view,
                native_logic_view,
                fields=("status", "result", "events"),
            )

        self._rollback_pending()
        install_count, installed_names = self._advance_historical_state(
            tx=tx,
            historical_result=record.historical_result,
            deployment_artifacts=deployment_artifacts,
        )
        artifact_install_count += install_count
        for name in installed_names:
            self._python_contract_exists[name] = True
            self._native_contract_readiness[name] = (True, None)

        return {
            "tx_hash": record.tx_hash,
            "height": record.height,
            "tx_index": record.tx_index,
            "contract": normalized_tx["payload"]["contract"],
            "function": normalized_tx["payload"]["function"],
            "python_ran": python_strict_view is not None,
            "native_ran": native_strict_view is not None,
            "python_logic_ran": python_logic_view is not None,
            "native_logic_ran": native_logic_view is not None,
            "python_strict_vs_historical": python_strict_mismatches,
            "native_strict_vs_historical": native_strict_mismatches,
            "python_logic_vs_historical": python_logic_mismatches,
            "native_logic_vs_historical": native_logic_mismatches,
            "python_strict_vs_native": python_native_strict_mismatches,
            "python_logic_vs_native": python_native_logic_mismatches,
            "python_error": python_error,
            "native_error": native_error,
            "skip_reason": skipped_reason,
            "deployment_artifacts_injected": deployment_artifacts is not None,
            "deployment_artifacts_error": deployment_error,
            "artifact_install_count": artifact_install_count,
            "historical": historical_view,
            "python_strict": python_strict_view,
            "native_strict": native_strict_view,
            "python_logic": python_logic_view,
            "native_logic": native_logic_view,
            "pseudo_state_patch": False,
        }


async def run_legacy_network_replay_audit(
    *,
    rpc_url: str,
    graphql_url: str,
    output_dir: Path,
    start_height: int | None = None,
    end_height: int | None = None,
    max_transactions: int | None = None,
    logic_only: bool = False,
    native_only: bool = False,
    progress_every: int = 500,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    graphql = LegacyNetworkGraphqlClient(graphql_url)
    rpc = CometBftRpcClient(rpc_url)
    harness = LegacyReplayHarness(storage_root=output_dir / "replay-state")

    try:
        latest_height = await rpc.latest_height()
        first_real_height = await graphql.fetch_first_real_tx_height()
        resolved_start = start_height or first_real_height
        resolved_end = min(end_height or latest_height, latest_height)

        genesis_entries = await graphql.fetch_genesis_state()
        runtime_code_inventory = _build_runtime_code_inventory(genesis_entries)
        contracts = await graphql.fetch_contract_inventory()
        transaction_inventory = await graphql.fetch_transaction_inventory()
        audit, incompatible_contracts = await audit_contract_inventory(
            contracts,
            runtime_code_inventory=runtime_code_inventory,
        )
        replay_heights = sorted(
            {
                record.block_height
                for record in transaction_inventory
                if resolved_start <= record.block_height <= resolved_end
            }
        )

        harness.seed_genesis(genesis_entries)
        genesis_install_count = harness.install_genesis_contracts(audit)

        report = {
            "network": {
                "rpc_url": rpc_url,
                "graphql_url": graphql_url,
                "latest_height": latest_height,
                "first_real_tx_height": first_real_height,
                "start_height": resolved_start,
                "end_height": resolved_end,
            },
            "genesis": {
                "entry_count": len(genesis_entries),
                "genesis_compatible_contract_installs": genesis_install_count,
            },
            "transaction_inventory": {
                "total": len(transaction_inventory),
                "heights_in_range": len(replay_heights),
            },
            "contracts": {
                "total": len(contracts),
                "compatible": sum(
                    1 for result in audit.values() if result.compatible
                ),
                "incompatible": len(incompatible_contracts),
            },
            "replay": {
                "processed": 0,
                "historical_success_total": 0,
                "historical_failure_total": 0,
                "python_strict_ran": 0,
                "native_strict_ran": 0,
                "python_logic_ran": 0,
                "native_logic_ran": 0,
                "python_strict_matches_historical": 0,
                "native_strict_matches_historical": 0,
                "python_logic_matches_historical": 0,
                "native_logic_matches_historical": 0,
                "historical_success_python_logic_ran": 0,
                "historical_success_native_logic_ran": 0,
                "historical_success_python_logic_matches": 0,
                "historical_success_native_logic_matches": 0,
                "python_strict_matches_native": 0,
                "python_logic_matches_native": 0,
                "deployment_artifacts_injected": 0,
                "artifact_installs": genesis_install_count,
                "state_patches_applied": 0,
                "skipped": 0,
            },
            "incompatible_contracts": incompatible_contracts,
            "mismatches": [],
        }

        jsonl_path = output_dir / "transactions.jsonl"
        logger.disable("xian.processor")
        logger.disable("xian.rewards")
        with jsonl_path.open("w", encoding="utf-8") as stream:
            try:
                async for record in iter_historical_transactions_graphql(
                    graphql,
                    start_height=resolved_start,
                    end_height=resolved_end,
                    max_transactions=max_transactions,
                ):
                    tx_report = harness.replay_record(
                        record,
                        logic_only=logic_only,
                        native_only=native_only,
                    )
                    if native_only:
                        tx_report["python_ran"] = False
                        tx_report["python_logic_ran"] = False
                        tx_report["python_strict_vs_historical"] = []
                        tx_report["python_logic_vs_historical"] = []
                        tx_report["python_strict_vs_native"] = []
                        tx_report["python_logic_vs_native"] = []
                        tx_report["python_strict"] = None
                        tx_report["python_logic"] = None
                    if logic_only:
                        tx_report["python_strict_vs_historical"] = []
                        tx_report["native_strict_vs_historical"] = []
                        tx_report["python_strict_vs_native"] = []
                        tx_report["python_ran"] = False
                        tx_report["native_ran"] = False
                        tx_report["python_strict"] = None
                        tx_report["native_strict"] = None

                    report["replay"]["processed"] += 1
                    if _historical_tx_succeeded(record.historical_result):
                        report["replay"]["historical_success_total"] += 1
                    else:
                        report["replay"]["historical_failure_total"] += 1
                    report["replay"]["python_strict_ran"] += int(
                        tx_report["python_ran"]
                    )
                    report["replay"]["native_strict_ran"] += int(
                        tx_report["native_ran"]
                    )
                    report["replay"]["python_logic_ran"] += int(
                        tx_report["python_logic_ran"]
                    )
                    report["replay"]["native_logic_ran"] += int(
                        tx_report["native_logic_ran"]
                    )
                    report["replay"]["deployment_artifacts_injected"] += int(
                        tx_report["deployment_artifacts_injected"]
                    )
                    report["replay"]["artifact_installs"] += int(
                        tx_report["artifact_install_count"]
                    )
                    report["replay"]["state_patches_applied"] += int(
                        tx_report["pseudo_state_patch"]
                    )
                    if tx_report["skip_reason"] is not None:
                        report["replay"]["skipped"] += 1
                    if not tx_report["python_strict_vs_historical"]:
                        report["replay"]["python_strict_matches_historical"] += int(
                            tx_report["python_ran"]
                        )
                    if not tx_report["native_strict_vs_historical"]:
                        report["replay"]["native_strict_matches_historical"] += int(
                            tx_report["native_ran"]
                        )
                    if not tx_report["python_logic_vs_historical"]:
                        report["replay"]["python_logic_matches_historical"] += int(
                            tx_report["python_logic_ran"]
                        )
                    if not tx_report["native_logic_vs_historical"]:
                        report["replay"]["native_logic_matches_historical"] += int(
                            tx_report["native_logic_ran"]
                        )
                    if _historical_tx_succeeded(record.historical_result):
                        report["replay"]["historical_success_python_logic_ran"] += int(
                            tx_report["python_logic_ran"]
                        )
                        report["replay"]["historical_success_native_logic_ran"] += int(
                            tx_report["native_logic_ran"]
                        )
                        if not tx_report["python_logic_vs_historical"]:
                            report["replay"]["historical_success_python_logic_matches"] += int(
                                tx_report["python_logic_ran"]
                            )
                        if not tx_report["native_logic_vs_historical"]:
                            report["replay"]["historical_success_native_logic_matches"] += int(
                                tx_report["native_logic_ran"]
                            )
                    if (
                        tx_report["python_ran"]
                        and tx_report["native_ran"]
                        and not tx_report["python_strict_vs_native"]
                    ):
                        report["replay"]["python_strict_matches_native"] += 1
                    if (
                        tx_report["python_logic_ran"]
                        and tx_report["native_logic_ran"]
                        and not tx_report["python_logic_vs_native"]
                    ):
                        report["replay"]["python_logic_matches_native"] += 1

                    compact_record = {
                        key: value
                        for key, value in tx_report.items()
                        if key
                        not in {
                            "historical",
                            "python_strict",
                            "native_strict",
                            "python_logic",
                            "native_logic",
                        }
                    }
                    compact_record["historical_success"] = (
                        _historical_tx_succeeded(record.historical_result)
                    )
                    stream.write(json.dumps(_json_safe(compact_record)) + "\n")

                    if (
                        not tx_report["pseudo_state_patch"]
                        and (
                        tx_report["python_strict_vs_historical"]
                        or tx_report["native_strict_vs_historical"]
                        or tx_report["python_logic_vs_historical"]
                        or tx_report["native_logic_vs_historical"]
                        or tx_report["python_strict_vs_native"]
                        or tx_report["python_logic_vs_native"]
                        or tx_report["python_error"] is not None
                        or tx_report["native_error"] is not None
                        )
                    ):
                        report["mismatches"].append(
                            {
                                "tx_hash": tx_report["tx_hash"],
                                "height": tx_report["height"],
                                "tx_index": tx_report["tx_index"],
                                "contract": tx_report["contract"],
                                "function": tx_report["function"],
                                "historical_success": _historical_tx_succeeded(
                                    record.historical_result
                                ),
                                "python_strict_vs_historical": tx_report[
                                    "python_strict_vs_historical"
                                ],
                                "native_strict_vs_historical": tx_report[
                                    "native_strict_vs_historical"
                                ],
                                "python_logic_vs_historical": tx_report[
                                    "python_logic_vs_historical"
                                ],
                                "native_logic_vs_historical": tx_report[
                                    "native_logic_vs_historical"
                                ],
                                "python_strict_vs_native": tx_report[
                                    "python_strict_vs_native"
                                ],
                                "python_logic_vs_native": tx_report[
                                    "python_logic_vs_native"
                                ],
                                "python_error": tx_report["python_error"],
                                "native_error": tx_report["native_error"],
                                "skip_reason": tx_report["skip_reason"],
                            }
                        )

                    processed = report["replay"]["processed"]
                    if progress_every > 0 and processed % progress_every == 0:
                        logger.info(
                            "legacy replay progress: processed={} "
                            "python_logic_matches={}/{} native_logic_matches={}/{} "
                            "historical_success_native_matches={}/{} mismatches={}",
                            processed,
                            report["replay"]["python_logic_matches_historical"],
                            report["replay"]["python_logic_ran"],
                            report["replay"]["native_logic_matches_historical"],
                            report["replay"]["native_logic_ran"],
                            report["replay"][
                                "historical_success_native_logic_matches"
                            ],
                            report["replay"][
                                "historical_success_native_logic_ran"
                            ],
                            len(report["mismatches"]),
                        )
            finally:
                logger.enable("xian.processor")
                logger.enable("xian.rewards")

        report_path = output_dir / "report.json"
        report_path.write_text(
            json.dumps(_json_safe(report), indent=2),
            encoding="utf-8",
        )
        (output_dir / "contract_inventory.json").write_text(
            json.dumps(
                [
                    {
                        "name": contract.name,
                        "tx_hash": contract.tx_hash,
                        "created": contract.created,
                    }
                    for contract in contracts
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        (output_dir / "contract_compatibility.json").write_text(
            json.dumps(
                [result.as_report_dict() for result in audit.values()],
                indent=2,
            ),
            encoding="utf-8",
        )
        return report
    finally:
        harness.close()
        await rpc.close()
        await graphql.close()


def build_summary_line(report: dict[str, Any]) -> str:
    replay = report["replay"]
    contracts = report["contracts"]
    network = report["network"]
    return (
        "legacy replay audit complete: "
        f"range={network['start_height']}-{network['end_height']} "
        f"processed={replay['processed']} "
        f"python_logic_matches={replay['python_logic_matches_historical']}/{replay['python_logic_ran']} "
        f"native_logic_matches={replay['native_logic_matches_historical']}/{replay['native_logic_ran']} "
        f"python_strict_matches={replay['python_strict_matches_historical']}/{replay['python_strict_ran']} "
        f"native_strict_matches={replay['native_strict_matches_historical']}/{replay['native_strict_ran']} "
        f"contracts_compatible={contracts['compatible']}/{contracts['total']} "
        f"mismatches={len(report['mismatches'])}"
    )
