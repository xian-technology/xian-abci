from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from contracting.storage.driver import Driver
from loguru import logger

from xian.constants import Constants
from xian.dashboard.app import normalize_rpc_url
from xian.services.bds.bds import BDS
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload
from xian.services.bds.runtime import resolve_bds_config
from xian.services.bds.serializer import utc_datetime
from xian.utils.cometbft import load_genesis_data, load_tendermint_config
from xian.utils.encoding import decode_transaction_bytes, hash_bytes
from xian.utils.state_patches import StatePatchManager, resolve_state_patch_dir


class RpcBlockSource(Protocol):
    async def latest_height(self) -> int: ...

    async def block(self, height: int) -> dict[str, Any]: ...

    async def block_results(self, height: int) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def parse_rfc3339_nano(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if "." not in value:
        return datetime.fromisoformat(value).astimezone(UTC)

    head, tail = value.split(".", 1)
    if "+" in tail:
        fraction, offset = tail.split("+", 1)
        offset = f"+{offset}"
    elif "-" in tail:
        fraction, offset = tail.split("-", 1)
        offset = f"-{offset}"
    else:
        fraction, offset = tail, ""

    normalized_fraction = fraction[:6].ljust(6, "0")
    normalized = f"{head}.{normalized_fraction}{offset}"
    return datetime.fromisoformat(normalized).astimezone(UTC)


def datetime_to_nanos(value: datetime) -> int:
    resolved = utc_datetime(value)
    seconds = int(resolved.timestamp())
    return (seconds * 1_000_000_000) + (resolved.microsecond * 1_000)


def resolve_rpc_url(
    constants: Constants,
    explicit_rpc_url: str | None = None,
) -> str:
    if explicit_rpc_url:
        return normalize_rpc_url(explicit_rpc_url)

    config = load_tendermint_config(constants)
    laddr = str(config.get("rpc", {}).get("laddr", "http://127.0.0.1:26657"))
    normalized = normalize_rpc_url(laddr)
    parts = urlsplit(normalized)
    host = parts.hostname or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        netloc = f"127.0.0.1:{parts.port or 26657}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return normalized


class CometBftRpcClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = normalize_rpc_url(rpc_url)
        self._session: aiohttp.ClientSession | None = None

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = await self._session_or_create()
        async with session.get(
            f"{self.rpc_url}/{path}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        return payload.get("result", payload)

    async def latest_height(self) -> int:
        status = await self._get("status")
        return int(status["sync_info"]["latest_block_height"])

    async def block(self, height: int) -> dict[str, Any]:
        return await self._get("block", {"height": str(height)})

    async def block_results(self, height: int) -> dict[str, Any]:
        return await self._get("block_results", {"height": str(height)})

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


@dataclass(frozen=True)
class ReindexPlan:
    latest_height: int
    indexed_height: int | None
    start_height: int
    end_height: int

    @property
    def total_blocks(self) -> int:
        if self.end_height < self.start_height:
            return 0
        return self.end_height - self.start_height + 1


class BdsReindexer:
    def __init__(
        self,
        *,
        bds: BDS,
        block_source: RpcBlockSource,
        state_patch_manager: StatePatchManager,
    ):
        self.bds = bds
        self.block_source = block_source
        self.state_patch_manager = state_patch_manager

    async def build_plan(
        self,
        *,
        start_height: int | None = None,
        end_height: int | None = None,
    ) -> ReindexPlan:
        latest_height = await self.block_source.latest_height()
        status = await self.bds.get_status(current_block_height=latest_height)
        indexed_height = status["indexed"]["indexed_height"]

        resolved_start = max(start_height or 1, 1)
        if start_height is None and isinstance(indexed_height, int):
            resolved_start = max(indexed_height + 1, 1)

        resolved_end = latest_height if end_height is None else end_height
        resolved_end = min(resolved_end, latest_height)

        return ReindexPlan(
            latest_height=latest_height,
            indexed_height=indexed_height,
            start_height=resolved_start,
            end_height=resolved_end,
        )

    async def reindex_range(self, plan: ReindexPlan) -> int:
        persisted = 0
        for height in range(plan.start_height, plan.end_height + 1):
            payload = await self.build_payload(height)
            if await self.bds.persist_block(payload):
                persisted += 1
            if persisted == 1 or persisted % 100 == 0:
                logger.info(
                    "BDS reindex progress: persisted block {} ({}/{})",
                    height,
                    persisted,
                    plan.total_blocks,
                )
        return persisted

    async def build_payload(self, height: int) -> BdsBlockPayload:
        block_response = await self.block_source.block(height)
        block_results = await self.block_source.block_results(height)

        block = block_response["block"]
        header = block["header"]
        block_time = parse_rfc3339_nano(header["time"])
        tx_bytes_list = self._extract_block_txs(block)
        tx_results = block_results.get("txs_results") or []
        if len(tx_bytes_list) != len(tx_results):
            raise ValueError(
                f"block {height} tx/result count mismatch: "
                f"{len(tx_bytes_list)} txs vs {len(tx_results)} results"
            )

        transactions: list[BdsTransactionPayload] = []
        for tx_index, (tx_b64, tx_result_rpc) in enumerate(
            zip(tx_bytes_list, tx_results, strict=True)
        ):
            payload = self._build_transaction_payload(
                tx_b64=tx_b64,
                tx_index=tx_index,
                tx_result_rpc=tx_result_rpc,
            )
            if payload is not None:
                transactions.append(payload)

        patch_hash, applied_patches = (
            self.state_patch_manager.build_applied_patches_for_block(height)
        )
        return BdsBlockPayload(
            block_meta={
                "height": int(header["height"]),
                "hash": str(block_response["block_id"]["hash"]).upper(),
                "nanos": datetime_to_nanos(block_time),
            },
            block_time=block_time,
            app_hash=str(header["app_hash"]).upper(),
            transactions=transactions,
            state_patches=applied_patches,
            state_patch_hash=patch_hash,
        )

    @staticmethod
    def _extract_block_txs(block: dict[str, Any]) -> list[str]:
        data = block.get("data", {})
        if isinstance(data, dict):
            txs = data.get("txs")
            return list(txs or [])
        if isinstance(data, list):
            return list(data)
        return []

    @staticmethod
    def _decode_tx_result_data(data_b64: str | None) -> dict[str, Any] | None:
        if not data_b64:
            return None
        decoded = base64.b64decode(data_b64).decode("utf-8")
        parsed = json.loads(decoded)
        return parsed if isinstance(parsed, dict) else None

    def _build_transaction_payload(
        self,
        *,
        tx_b64: str,
        tx_index: int,
        tx_result_rpc: dict[str, Any],
    ) -> BdsTransactionPayload | None:
        raw_tx = base64.b64decode(tx_b64)
        envelope, _ = decode_transaction_bytes(raw_tx)
        decoded_result = self._decode_tx_result_data(tx_result_rpc.get("data"))

        if not self._is_indexable_tx_result(decoded_result):
            return None

        tx_hash = hash_bytes(raw_tx).upper()
        tx_result = dict(decoded_result)
        tx_result["hash"] = tx_hash
        tx_result["status"] = int(
            tx_result.get("status", tx_result_rpc["code"])
        )
        tx_result["stamps_used"] = int(
            tx_result.get("stamps_used", tx_result_rpc.get("gas_used", 0))
        )
        tx_result.setdefault("state", [])
        tx_result.setdefault("events", [])
        tx_result.setdefault("rewards", {})
        return BdsTransactionPayload(
            tx_index=tx_index,
            envelope={key: value for key, value in envelope.items()},
            payload=envelope["payload"],
            tx_result=tx_result,
        )

    @staticmethod
    def _is_indexable_tx_result(
        tx_result: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(tx_result, dict):
            return False
        return {
            "status",
            "state",
            "stamps_used",
        }.issubset(tx_result.keys())


async def run_bds_reindex(
    *,
    constants: Constants,
    rpc_url: str | None = None,
    start_height: int | None = None,
    end_height: int | None = None,
    reset: bool = False,
) -> ReindexPlan:
    cometbft_genesis = load_genesis_data(constants)
    bds_config = resolve_bds_config(constants)
    bds = BDS(config=bds_config)
    await bds.initialize_storage(cometbft_genesis, reset=reset)

    state_patch_manager = StatePatchManager(
        Driver(storage_home=constants.STORAGE_HOME)
    )
    patch_dir_path = resolve_state_patch_dir(constants)
    state_patch_manager.load_patches(str(patch_dir_path))

    block_source = CometBftRpcClient(resolve_rpc_url(constants, rpc_url))
    reindexer = BdsReindexer(
        bds=bds,
        block_source=block_source,
        state_patch_manager=state_patch_manager,
    )
    try:
        plan = await reindexer.build_plan(
            start_height=start_height,
            end_height=end_height,
        )
        if plan.total_blocks == 0:
            logger.info(
                "BDS reindex is already caught up at height {}",
                plan.indexed_height,
            )
            return plan
        logger.info(
            "Starting BDS reindex from block {} to {} via {}",
            plan.start_height,
            plan.end_height,
            block_source.rpc_url,
        )
        await reindexer.reindex_range(plan)
        return plan
    finally:
        await block_source.close()
        await bds.close()
