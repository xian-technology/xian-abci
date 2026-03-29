from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from timeit import default_timer as timer
from typing import Any

from loguru import logger

from xian.constants import Constants
from xian.services.bds import sql
from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload
from xian.services.bds.serializer import (
    canonical_decimal,
    canonical_json_text,
    canonical_json_value,
    canonical_result_value,
    utc_datetime,
)

GENESIS_BLOCK_HASH = "GENESIS"
GENESIS_TX_HASH = "GENESIS"
GENESIS_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _jsonb_param(value: Any) -> str:
    return canonical_json_text(value)


def _nullable_jsonb_param(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_json_text(value)


class BDS:
    WORKER_RETRY_DELAY_SECONDS = 2.0
    CATCHUP_RETRY_DELAY_SECONDS = 2.0
    CLOSE_FLUSH_TIMEOUT_SECONDS = 1.0

    def __init__(self, config: BdsConfig, raw_driver=None):
        self.config = config
        self.raw_driver = raw_driver
        self.db = DB(config)
        self.spool_dir = Path(config.spool_dir or ".bds-spool")
        self._pending_payloads: dict[int, BdsBlockPayload] = {}
        self._pending_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._catchup_task: asyncio.Task | None = None
        self._indexed_height: int | None = None
        self._last_enqueue_error: dict[str, Any] | None = None
        self._block_source = None
        self._reindexer = None

    async def open_storage(self) -> None:
        await self.db.init_pool()
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    async def reset_schema(self) -> None:
        await self.db.execute(sql.drop_all_tables())
        await self._prepare_schema()

    async def ensure_schema(self) -> None:
        await self._prepare_schema()

    async def initialize_storage(
        self,
        cometbft_genesis: dict,
        *,
        reset: bool = False,
    ) -> None:
        await self.open_storage()
        if reset:
            self.clear_spool()
            await self.reset_schema()
        else:
            await self.ensure_schema()

        if not await self.db.has_entries("blocks"):
            await self.process_genesis_block(cometbft_genesis)

    async def init(self, cometbft_genesis: dict):
        await self.initialize_storage(cometbft_genesis)
        await self.start()
        logger.info("BDS service initialized")
        return self

    async def start(self) -> None:
        await self._refresh_indexed_height()
        self._start_worker()
        await self._replay_spool()
        await self._start_catchup()

    def _start_worker(self) -> None:
        if self._worker_task is not None:
            return

        self._worker_task = asyncio.create_task(
            self._run_worker(), name="xian-bds-worker"
        )

    async def _run_worker(self) -> None:
        while True:
            await self._pending_event.wait()
            try:
                payload = self._pop_next_pending_payload()
                if payload is None:
                    self._pending_event.clear()
                    continue
                while True:
                    try:
                        if await self.persist_block(payload):
                            self._indexed_height = int(
                                payload.block_meta["height"]
                            )
                            break
                        await asyncio.sleep(self.WORKER_RETRY_DELAY_SECONDS)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception(
                            f"BDS worker failed to persist block: {exc}"
                        )
                        await asyncio.sleep(self.WORKER_RETRY_DELAY_SECONDS)
            except asyncio.CancelledError:
                raise
            finally:
                if self._has_next_pending_payload():
                    self._pending_event.set()
                else:
                    self._pending_event.clear()

    async def enqueue_block(self, payload: BdsBlockPayload) -> None:
        if self._worker_task is None:
            raise RuntimeError("BDS worker is not initialized")
        if not self._enqueue_pending_payload(payload):
            self._record_enqueue_error(
                "pending_buffer_full",
                "BDS pending buffer is full; block will be recovered via catch-up",
            )

    async def _replay_spool(self) -> None:
        for spool_path in self._pending_spool_files():
            self._enqueue_pending_payload(self._read_spool_file(spool_path))

    async def flush(self) -> None:
        if self._worker_task is None:
            return
        while self._pending_payloads:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(
                    self.flush(), timeout=self.CLOSE_FLUSH_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for BDS queue flush; pending blocks remain in {}",
                    self.spool_dir,
                )
        if self._catchup_task is not None:
            self._catchup_task.cancel()
            await asyncio.gather(self._catchup_task, return_exceptions=True)
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None
        self._catchup_task = None
        self._pending_payloads.clear()
        self._pending_event.clear()
        if self._block_source is not None:
            await self._block_source.close()
            self._block_source = None
        self._reindexer = None
        await self.db.close_pool()

    def _spool_file_path(self, payload: BdsBlockPayload) -> Path:
        height = int(payload.block_meta["height"])
        block_hash = str(payload.block_meta["hash"])
        return self.spool_dir / f"{height:020d}-{block_hash}.json"

    def _write_spool_file(self, payload: BdsBlockPayload) -> Path:
        spool_path = self._spool_file_path(payload)
        temp_path = spool_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload.to_spool_dict(), separators=(",", ":")),
            encoding="utf-8",
        )
        temp_path.replace(spool_path)
        return spool_path

    def _read_spool_file(self, spool_path: Path) -> BdsBlockPayload:
        return BdsBlockPayload.from_spool_dict(
            json.loads(spool_path.read_text(encoding="utf-8"))
        )

    def _pending_spool_files(self) -> list[Path]:
        return sorted(self.spool_dir.glob("*.json"))

    def clear_spool(self) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        for path in self.spool_dir.glob("*.json*"):
            path.unlink(missing_ok=True)

    def _record_enqueue_error(self, code: str, message: str) -> None:
        self._last_enqueue_error = {
            "code": code,
            "message": message,
            "recorded_at": utc_datetime(datetime.now()).isoformat(),
        }

    def _clear_enqueue_error(self) -> None:
        self._last_enqueue_error = None

    def _enqueue_pending_payload(self, payload: BdsBlockPayload) -> bool:
        height = int(payload.block_meta["height"])
        if self._indexed_height is not None and height <= self._indexed_height:
            return True
        if height in self._pending_payloads:
            return True
        if len(self._pending_payloads) >= max(self.config.queue_max_size, 1):
            return False
        self._pending_payloads[height] = payload
        self._pending_event.set()
        self._clear_enqueue_error()
        return True

    def _next_expected_height(self) -> int:
        if self._indexed_height is None:
            return 0
        return int(self._indexed_height) + 1

    def _has_next_pending_payload(self) -> bool:
        return self._next_expected_height() in self._pending_payloads

    def _pop_next_pending_payload(self) -> BdsBlockPayload | None:
        return self._pending_payloads.pop(self._next_expected_height(), None)

    def _spool_file_height(self, spool_path: Path) -> int | None:
        prefix, _, _ = spool_path.name.partition("-")
        try:
            return int(prefix)
        except ValueError:
            try:
                return int(
                    self._read_spool_file(spool_path).block_meta["height"]
                )
            except Exception:
                return None

    async def compact_spool(self) -> dict[str, Any]:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        indexed_height = await self.db.fetchval(
            "SELECT MAX(height) FROM blocks"
        )
        removed_files = 0
        removed_bytes = 0
        removed_temp_files = 0

        for temp_path in self.spool_dir.glob("*.json.tmp"):
            removed_temp_files += 1
            temp_path.unlink(missing_ok=True)

        if indexed_height is None:
            return {
                "indexed_height": None,
                "removed_files": removed_files,
                "removed_bytes": removed_bytes,
                "removed_temp_files": removed_temp_files,
                "kept_files": len(self._pending_spool_files()),
            }

        for spool_path in self._pending_spool_files():
            spool_height = self._spool_file_height(spool_path)
            if spool_height is None or spool_height > int(indexed_height):
                continue
            removed_bytes += spool_path.stat().st_size
            removed_files += 1
            spool_path.unlink(missing_ok=True)

        return {
            "indexed_height": int(indexed_height),
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "removed_temp_files": removed_temp_files,
            "kept_files": len(self._pending_spool_files()),
        }

    async def drain_spool(
        self, *, timeout_seconds: float = 60.0
    ) -> dict[str, Any]:
        worker_started = False
        if self._worker_task is None:
            self._start_worker()
            await self._replay_spool()
            worker_started = True

        timed_out = False
        try:
            await asyncio.wait_for(self.flush(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True

        compacted = await self.compact_spool()
        status = await self.get_status()
        return {
            "worker_started": worker_started,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "compacted": compacted,
            "status": status,
        }

    def _spool_entry_metadata(
        self, spool_path: Path, payload: BdsBlockPayload | None = None
    ) -> dict[str, Any]:
        loaded_payload = payload or self._read_spool_file(spool_path)
        return {
            "file": spool_path.name,
            "size_bytes": spool_path.stat().st_size,
            "block_height": int(loaded_payload.block_meta["height"]),
            "block_hash": str(loaded_payload.block_meta["hash"]),
            "block_time": utc_datetime(loaded_payload.block_time).isoformat(),
            "tx_count": len(loaded_payload.transactions),
            "state_patch_count": len(loaded_payload.state_patches),
            "app_hash": loaded_payload.app_hash,
        }

    async def get_status(
        self, *, current_block_height: int | None = None
    ) -> dict[str, Any]:
        pending_spool = self._pending_spool_files()
        spool_total_bytes = sum(
            spool_path.stat().st_size for spool_path in pending_spool
        )
        oldest_pending = (
            self._spool_entry_metadata(pending_spool[0])
            if pending_spool
            else None
        )
        newest_pending = (
            self._spool_entry_metadata(pending_spool[-1])
            if pending_spool
            else None
        )

        db_status = "ok"
        db_error = None
        indexed = {
            "indexed_block_count": 0,
            "indexed_height": None,
            "indexed_block_hash": None,
            "indexed_block_time": None,
            "indexed_block_time_iso": None,
            "indexed_tx_count": None,
            "indexed_app_hash": None,
        }
        try:
            row = await self.db.fetchrow(sql.select_index_status())
            if row is not None:
                indexed.update(dict(row))
        except Exception as exc:
            db_status = "error"
            db_error = f"{type(exc).__name__}: {exc}"

        indexed_height = indexed["indexed_height"]
        height_lag = None
        if isinstance(current_block_height, int) and isinstance(
            indexed_height, int
        ):
            height_lag = max(current_block_height - indexed_height, 0)

        queue_depth = len(self._pending_payloads)
        queue_capacity = max(self.config.queue_max_size, 1)
        disk_usage = shutil.disk_usage(self.spool_dir)

        alerts: list[dict[str, Any]] = []
        if db_status != "ok":
            alerts.append(
                {
                    "level": "error",
                    "code": "db_unavailable",
                    "message": "BDS database status is degraded",
                }
            )
        if len(pending_spool) >= self.config.spool_warn_entries:
            alerts.append(
                {
                    "level": "warning",
                    "code": "spool_entries_high",
                    "message": "BDS spool entry count exceeded warning threshold",
                    "threshold": self.config.spool_warn_entries,
                    "value": len(pending_spool),
                }
            )
        if spool_total_bytes >= self.config.spool_warn_bytes:
            alerts.append(
                {
                    "level": "warning",
                    "code": "spool_bytes_high",
                    "message": "BDS spool size exceeded warning threshold",
                    "threshold": self.config.spool_warn_bytes,
                    "value": spool_total_bytes,
                }
            )
        if disk_usage.free <= self.config.disk_free_warn_bytes:
            alerts.append(
                {
                    "level": "warning",
                    "code": "disk_free_low",
                    "message": "Low free disk space on BDS spool filesystem",
                    "threshold": self.config.disk_free_warn_bytes,
                    "value": disk_usage.free,
                }
            )

        has_spool_backlog = len(pending_spool) > 0
        has_height_lag = isinstance(height_lag, int) and height_lag > 0

        return {
            "worker_running": self._worker_task is not None
            and not self._worker_task.done(),
            "catchup_running": self._catchup_task is not None
            and not self._catchup_task.done(),
            "queue_depth": queue_depth,
            "queue_capacity": queue_capacity,
            "queue_utilization": queue_depth / queue_capacity,
            "spool_dir": str(self.spool_dir),
            "spool_pending_count": len(pending_spool),
            "spool_total_bytes": spool_total_bytes,
            "spool_oldest_pending": oldest_pending,
            "spool_newest_pending": newest_pending,
            "storage": {
                "filesystem_total_bytes": disk_usage.total,
                "filesystem_used_bytes": disk_usage.used,
                "filesystem_free_bytes": disk_usage.free,
            },
            "db_status": db_status,
            "db_error": db_error,
            "last_enqueue_error": self._last_enqueue_error,
            "indexed": indexed,
            "current_block_height": current_block_height,
            "height_lag": height_lag,
            "catching_up": has_spool_backlog or has_height_lag,
            "alerts": alerts,
        }

    async def _refresh_indexed_height(self) -> int | None:
        indexed_height = await self.db.fetchval(
            "SELECT MAX(height) FROM blocks"
        )
        self._indexed_height = (
            int(indexed_height) if indexed_height is not None else None
        )
        return self._indexed_height

    async def _start_catchup(self) -> None:
        if not self.config.catchup_enabled or self._catchup_task is not None:
            return
        if not self.config.rpc_url:
            return
        await self._ensure_catchup_runtime()
        self._catchup_task = asyncio.create_task(
            self._run_catchup(), name="xian-bds-catchup"
        )

    async def _ensure_catchup_runtime(self) -> None:
        if self._block_source is not None and self._reindexer is not None:
            return
        from xian.services.bds.reindex import BdsReindexer, CometBftRpcClient
        from xian.utils.state_patches import (
            StatePatchManager,
            resolve_state_patch_dir,
        )

        state_patch_manager = StatePatchManager(self.raw_driver)
        patch_dir_path = resolve_state_patch_dir(Constants)
        state_patch_manager.load_patches(str(patch_dir_path))
        self._block_source = CometBftRpcClient(self.config.rpc_url)
        self._reindexer = BdsReindexer(
            bds=self,
            block_source=self._block_source,
            state_patch_manager=state_patch_manager,
        )

    async def _run_catchup(self) -> None:
        while True:
            try:
                await self._catch_up_once()
                await asyncio.sleep(self.config.catchup_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"BDS catch-up failed: {exc}")
                await asyncio.sleep(self.CATCHUP_RETRY_DELAY_SECONDS)

    async def _catch_up_once(self) -> None:
        if not self.config.catchup_enabled:
            return
        await self._ensure_catchup_runtime()
        latest_height = await self._block_source.latest_height()
        indexed_height = (
            self._indexed_height if self._indexed_height is not None else 0
        )
        highest_pending = max(self._pending_payloads, default=indexed_height)
        target_height = max(int(latest_height), int(highest_pending))
        next_height = indexed_height + 1

        while next_height <= target_height:
            if len(self._pending_payloads) >= max(
                self.config.queue_max_size, 1
            ):
                return
            if next_height in self._pending_payloads:
                next_height += 1
                continue
            payload = await self._reindexer.build_payload(next_height)
            if not self._enqueue_pending_payload(payload):
                return
            next_height += 1

    async def get_spool_entries(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        pending_spool = self._pending_spool_files()
        selected = pending_spool[offset : offset + limit]
        return [
            self._spool_entry_metadata(spool_path) for spool_path in selected
        ]

    async def _prepare_schema(self) -> None:
        await self.db.execute(sql.create_meta())
        current_version = await self.db.fetchval(sql.select_schema_version())
        if current_version != str(sql.SCHEMA_VERSION):
            if (
                current_version is not None
                or await self._legacy_tables_present()
            ):
                logger.warning(
                    "Resetting BDS schema to version {}",
                    sql.SCHEMA_VERSION,
                )
                await self.db.execute(sql.drop_all_tables())
            await self.db.execute(sql.create_meta())

        for statement in (
            sql.create_blocks(),
            sql.create_transactions(),
            sql.create_state_changes(),
            sql.create_state(),
            sql.create_events(),
            sql.create_rewards(),
            sql.create_contracts(),
            sql.create_state_patches(),
        ):
            await self.db.execute(statement)

        await self.db.execute(
            sql.upsert_schema_version(),
            [str(sql.SCHEMA_VERSION), utc_datetime(datetime.now())],
        )

    async def _legacy_tables_present(self) -> bool:
        return bool(
            await self.db.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                        'transactions',
                        'state',
                        'state_changes',
                        'events',
                        'rewards',
                        'contracts',
                        'state_patches',
                        'addresses'
                      )
                );
                """
            )
        )

    async def process_genesis_block(self, cometbft_genesis: dict):
        start_time = timer()
        genesis_state = cometbft_genesis["abci_genesis"]["genesis"]
        block_time = GENESIS_CREATED_AT

        async with self.db.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    sql.insert_block(),
                    0,
                    GENESIS_BLOCK_HASH,
                    0,
                    block_time,
                    1,
                    GENESIS_BLOCK_HASH,
                    block_time,
                )
                await connection.execute(
                    sql.insert_transaction(),
                    GENESIS_TX_HASH,
                    0,
                    GENESIS_BLOCK_HASH,
                    0,
                    0,
                    "sys",
                    0,
                    "GENESIS_SUBMISSION",
                    "process_genesis_block",
                    True,
                    0,
                    0,
                    _jsonb_param(
                        {"status": "ok", "genesis_entries": len(genesis_state)}
                    ),
                    _jsonb_param({"kind": "genesis"}),
                    _jsonb_param(
                        {
                            "kind": "genesis",
                            "genesis": canonical_json_value(genesis_state),
                        }
                    ),
                    block_time,
                )

                current_index: dict[str, tuple[int | None, str | None]] = {}
                for write_index, state in enumerate(genesis_state):
                    key = state["key"]
                    value = _jsonb_param(state["value"])
                    previous_change_id, previous_tx_hash = current_index.get(
                        key, (None, None)
                    )
                    change_id = await connection.fetchval(
                        sql.insert_state_change(),
                        0,
                        GENESIS_BLOCK_HASH,
                        0,
                        GENESIS_TX_HASH,
                        0,
                        write_index,
                        key,
                        value,
                        previous_change_id,
                        previous_tx_hash,
                        "genesis",
                        block_time,
                    )
                    current_index[key] = (change_id, GENESIS_TX_HASH)
                    await connection.execute(
                        sql.upsert_state(),
                        key,
                        value,
                        change_id,
                        GENESIS_TX_HASH,
                        0,
                        block_time,
                    )

                for contract_name, record in self._collect_contract_records(
                    genesis_state
                ).items():
                    display_source = record["source"] or record["code"]
                    if display_source is None:
                        continue
                    submission_time = record[
                        "submitted_at"
                    ] or self.get_submission_time(genesis_state, contract_name)
                    await connection.execute(
                        sql.upsert_contract(),
                        contract_name,
                        GENESIS_TX_HASH,
                        0,
                        submission_time,
                        display_source,
                        self.is_XSC0001(display_source),
                    )

        logger.debug(
            f"Saved genesis block to BDS in {timer() - start_time:.3f} seconds"
        )

    async def persist_block(self, payload: BdsBlockPayload) -> bool:
        start_time = timer()
        created_at = utc_datetime(payload.block_time)
        state_patches = payload.state_patches or []

        try:
            already_persisted = await self.db.fetchval(
                "SELECT 1 FROM blocks WHERE height = $1",
                [payload.block_meta["height"]],
            )
            if already_persisted:
                return True

            async with self.db.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        sql.insert_block(),
                        payload.block_meta["height"],
                        payload.block_meta["hash"],
                        payload.block_meta["nanos"],
                        created_at,
                        len(payload.transactions),
                        payload.app_hash,
                        created_at,
                    )

                    touched_keys = {
                        state_change["key"]
                        for tx in payload.transactions
                        for state_change in tx.tx_result["state"]
                    }
                    touched_keys.update(
                        patch["key"]
                        for patch in state_patches
                        if "key" in patch
                    )
                    current_index = await self._load_current_state_index(
                        connection, touched_keys
                    )

                    for tx in payload.transactions:
                        await self._persist_transaction(
                            connection,
                            block_meta=payload.block_meta,
                            block_time=created_at,
                            current_index=current_index,
                            tx=tx,
                        )

                    if state_patches:
                        await self._persist_state_patches(
                            connection,
                            block_meta=payload.block_meta,
                            block_time=created_at,
                            current_index=current_index,
                            state_patches=state_patches,
                            state_patch_hash=payload.state_patch_hash
                            or f"STATE_PATCH_{payload.block_meta['height']}",
                            tx_index=len(payload.transactions),
                        )
        except Exception as exc:
            logger.exception(f"Failed to persist block to BDS: {exc}")
            return False

        logger.debug(
            "Saved block {} to BDS in {:.3f} seconds",
            payload.block_meta["height"],
            timer() - start_time,
        )
        return True

    async def _persist_transaction(
        self,
        connection,
        *,
        block_meta: dict[str, Any],
        block_time: datetime,
        current_index: dict[str, tuple[int | None, str | None]],
        tx: BdsTransactionPayload,
    ) -> None:
        tx_result = tx.tx_result
        payload = tx.payload
        tx_hash = tx_result["hash"]
        tx_index = int(tx.tx_index)

        await connection.execute(
            sql.insert_transaction(),
            tx_hash,
            block_meta["height"],
            block_meta["hash"],
            block_meta["nanos"],
            tx_index,
            payload["sender"],
            payload["nonce"],
            payload["contract"],
            payload["function"],
            tx_result["status"] == 0,
            tx_result["status"],
            tx_result["stamps_used"],
            _nullable_jsonb_param(
                canonical_result_value(tx_result.get("result"))
            ),
            _jsonb_param(payload),
            _jsonb_param(tx.envelope),
            block_time,
        )

        for write_index, state_change in enumerate(tx_result["state"]):
            key = state_change["key"]
            value = _jsonb_param(state_change["value"])
            previous_change_id, previous_tx_hash = current_index.get(
                key, (None, None)
            )
            change_id = await connection.fetchval(
                sql.insert_state_change(),
                block_meta["height"],
                block_meta["hash"],
                block_meta["nanos"],
                tx_hash,
                tx_index,
                write_index,
                key,
                value,
                previous_change_id,
                previous_tx_hash,
                "transaction",
                block_time,
            )
            current_index[key] = (change_id, tx_hash)
            await connection.execute(
                sql.upsert_state(),
                key,
                value,
                change_id,
                tx_hash,
                block_meta["height"],
                block_time,
            )

        reward_index = 0
        reward_records = tx_result.get("reward_records") or []
        if reward_records:
            for record in reward_records:
                await connection.execute(
                    sql.insert_reward(),
                    block_meta["height"],
                    tx_hash,
                    tx_index,
                    reward_index,
                    str(record.get("type", "")),
                    record.get("recipient_key"),
                    record.get("source_contract"),
                    canonical_decimal(record.get("value", 0)),
                    block_time,
                )
                reward_index += 1
        else:
            reward_groups = tx_result.get("rewards") or {}
            for reward_type, rewards in reward_groups.items():
                for recipient_key, value in rewards.items():
                    await connection.execute(
                        sql.insert_reward(),
                        block_meta["height"],
                        tx_hash,
                        tx_index,
                        reward_index,
                        reward_type,
                        recipient_key,
                        None,
                        canonical_decimal(value),
                        block_time,
                    )
                    reward_index += 1

        for event_index, event in enumerate(tx_result.get("events", [])):
            await connection.execute(
                sql.insert_event(),
                block_meta["height"],
                tx_hash,
                tx_index,
                event_index,
                str(event.get("contract", "")),
                str(event.get("event", "ContractEvent")),
                str(event.get("signer", "")),
                str(event.get("caller", "")),
                _jsonb_param(event.get("data_indexed", {})),
                _jsonb_param(event.get("data", {})),
                block_time,
            )

        if tx_result["status"] == 0:
            for contract_name, record in self._collect_contract_records(
                tx_result.get("state", [])
            ).items():
                display_source = record["source"] or record["code"]
                if display_source is None:
                    continue
                submission_time = record["submitted_at"] or block_time
                await connection.execute(
                    sql.upsert_contract(),
                    contract_name,
                    tx_hash,
                    block_meta["height"],
                    submission_time,
                    display_source,
                    self.is_XSC0001(display_source),
                )

    async def _persist_state_patches(
        self,
        connection,
        *,
        block_meta: dict[str, Any],
        block_time: datetime,
        current_index: dict[str, tuple[int | None, str | None]],
        state_patches: list[dict[str, Any]],
        state_patch_hash: str,
        tx_index: int,
    ) -> None:
        flattened_changes = []
        for execution in state_patches:
            for change in execution.get("changes", []):
                flattened_changes.append(
                    {
                        "patch_id": execution.get("patch_id"),
                        "proposal_id": execution.get("proposal_id"),
                        "bundle_hash": execution.get("bundle_hash"),
                        "execution_hash": execution.get("execution_hash"),
                        "activation_height": execution.get("activation_height"),
                        "governance_contract": execution.get(
                            "governance_contract"
                        ),
                        "emergency": execution.get("emergency", False),
                        "key": change["key"],
                        "value": change["value"],
                        "comment": change.get("comment", ""),
                    }
                )

        await connection.execute(
            sql.insert_transaction(),
            state_patch_hash,
            block_meta["height"],
            block_meta["hash"],
            block_meta["nanos"],
            tx_index,
            "sys",
            0,
            "STATE_PATCHER",
            "STATE_PATCH",
            True,
            0,
            0,
            _jsonb_param(
                {
                    "bundle_count": len(state_patches),
                    "patch_count": len(flattened_changes),
                    "comment": "Governed state patch pseudo-transaction",
                }
            ),
            _jsonb_param({"kind": "state_patch"}),
            _jsonb_param(
                {
                    "kind": "state_patch",
                    "executions": canonical_json_value(state_patches),
                }
            ),
            block_time,
        )

        for write_index, change in enumerate(flattened_changes):
            key = change["key"]
            value = _jsonb_param(change["value"])
            previous_change_id, previous_tx_hash = current_index.get(
                key, (None, None)
            )
            change_id = await connection.fetchval(
                sql.insert_state_change(),
                block_meta["height"],
                block_meta["hash"],
                block_meta["nanos"],
                state_patch_hash,
                tx_index,
                write_index,
                key,
                value,
                previous_change_id,
                previous_tx_hash,
                "state_patch",
                block_time,
            )
            current_index[key] = (change_id, state_patch_hash)
            await connection.execute(
                sql.upsert_state(),
                key,
                value,
                change_id,
                state_patch_hash,
                block_meta["height"],
                block_time,
            )

        await connection.execute(
            sql.insert_state_patch_record(),
            state_patch_hash,
            block_meta["height"],
            block_meta["hash"],
            block_meta["nanos"],
            len(flattened_changes),
            _jsonb_param(state_patches),
            block_time,
        )

    async def _load_current_state_index(
        self,
        connection,
        keys: set[str],
    ) -> dict[str, tuple[int | None, str | None]]:
        if not keys:
            return {}

        rows = await connection.fetch(
            """
            SELECT key, last_change_id, last_tx_hash
            FROM state
            WHERE key = ANY($1::text[]);
            """,
            list(keys),
        )
        return {
            row["key"]: (row["last_change_id"], row["last_tx_hash"])
            for row in rows
        }

    async def get_contracts(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_contracts(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_contract_summary(self, contract_name: str):
        row = await self.db.fetchrow(
            sql.select_contract_summary(), [contract_name]
        )
        return dict(row) if row is not None else None

    async def get_blocks(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_blocks(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_block(self, block_height: int):
        row = await self.db.fetchrow(
            sql.select_block_by_height(), [block_height]
        )
        return dict(row) if row is not None else None

    async def get_block_by_hash(self, block_hash: str):
        row = await self.db.fetchrow(sql.select_block_by_hash(), [block_hash])
        return dict(row) if row is not None else None

    async def get_tx(self, tx_hash: str):
        row = await self.db.fetchrow(
            sql.select_transaction_by_hash(), [tx_hash]
        )
        return dict(row) if row is not None else None

    async def get_txs_for_block(self, block_ref: str):
        if len(block_ref) == 64:
            rows = await self.db.fetch(
                sql.select_transactions_for_block_hash(), [block_ref]
            )
        else:
            rows = await self.db.fetch(
                sql.select_transactions_for_block_height(), [int(block_ref)]
            )
        return [dict(row) for row in rows]

    async def get_txs_by_sender(
        self, sender: str, limit: int = 100, offset: int = 0
    ):
        rows = await self.db.fetch(
            sql.select_transactions_by_sender(), [sender, limit, offset]
        )
        return [dict(row) for row in rows]

    async def get_recent_addresses(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(
            sql.select_recent_addresses(), [limit, offset]
        )
        return [dict(row) for row in rows]

    async def get_txs_by_contract(
        self, contract: str, limit: int = 100, offset: int = 0
    ):
        rows = await self.db.fetch(
            sql.select_transactions_by_contract(), [contract, limit, offset]
        )
        return [dict(row) for row in rows]

    async def get_events_for_tx(self, tx_hash: str):
        rows = await self.db.fetch(sql.select_events_for_tx(), [tx_hash])
        return [dict(row) for row in rows]

    async def get_events(
        self,
        contract: str,
        event: str,
        limit: int = 100,
        offset: int = 0,
        *,
        after_id: int | None = None,
    ):
        if after_id is not None:
            rows = await self.db.fetch(
                sql.select_events_by_contract_event_after_id(),
                [contract, event, after_id, limit],
            )
        else:
            rows = await self.db.fetch(
                sql.select_events_by_contract_event(),
                [contract, event, limit, offset],
            )
        return [dict(row) for row in rows]

    async def get_recent_events(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_recent_events(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_state(self, key: str, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_state(), [key, limit, offset])
        return [dict(row) for row in rows]

    async def get_state_history(
        self, key: str, limit: int = 100, offset: int = 0
    ):
        rows = await self.db.fetch(
            sql.select_state_history(), [key, limit, offset]
        )
        return [dict(row) for row in rows]

    async def get_state_for_tx(self, tx_hash: str):
        rows = await self.db.fetch(sql.select_state_tx(), [tx_hash])
        return [dict(row) for row in rows]

    async def get_state_for_block(self, key: str):
        if len(key) == 64:
            rows = await self.db.fetch(sql.select_state_block_hash(), [key])
        else:
            rows = await self.db.fetch(
                sql.select_state_block_height(), [int(key)]
            )
        return [dict(row) for row in rows]

    async def get_state_patches(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_state_patches(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_state_patches_for_block(self, block_height: int):
        rows = await self.db.fetch(
            sql.select_state_patches_for_block(), [block_height]
        )
        return [dict(row) for row in rows]

    async def get_state_patch_by_hash(self, patch_hash: str):
        row = await self.db.fetchrow(
            sql.select_state_patch_by_hash(), [patch_hash]
        )
        return dict(row) if row is not None else None

    async def get_state_changes_for_patch(self, patch_hash: str):
        rows = await self.db.fetch(
            sql.select_state_changes_for_patch(), [patch_hash]
        )
        return [dict(row) for row in rows]

    async def get_developer_rewards(self, recipient_key: str):
        row = await self.db.fetchrow(
            sql.select_developer_rewards_summary(), [recipient_key]
        )
        return dict(row) if row is not None else None

    def is_XSC0001(self, code: str) -> bool:
        normalized = code.replace(" ", "")
        if "balances=Hash(" not in normalized:
            return False
        if "@export\ndeftransfer(amount:float,to:str):" not in normalized:
            return False
        if "@export\ndefapprove(amount:float,to:str):" not in normalized:
            return False
        if (
            "@export\ndeftransfer_from(amount:float,to:str,main_account:str):"
            not in normalized
        ):
            return False
        return True

    def get_submission_time(
        self, genesis_state: list[dict[str, Any]], contract_name: str
    ) -> datetime:
        for item in genesis_state:
            if "con_" not in contract_name:
                if contract_name == "submission":
                    return GENESIS_CREATED_AT
                return datetime(1970, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            if (
                isinstance(item, dict)
                and item.get("key") == f"{contract_name}.__submitted__"
            ):
                time_value = item["value"].get("__time__")
                return utc_datetime(datetime(*time_value))
        return GENESIS_CREATED_AT

    def _submission_time_from_state_value(self, value: Any) -> datetime | None:
        if isinstance(value, dict):
            time_value = value.get("__time__")
            if isinstance(time_value, list | tuple) and len(time_value) >= 6:
                return utc_datetime(datetime(*time_value[:6]))

        if all(
            hasattr(value, attribute)
            for attribute in (
                "year",
                "month",
                "day",
                "hour",
                "minute",
                "second",
            )
        ):
            return utc_datetime(
                datetime(
                    value.year,
                    value.month,
                    value.day,
                    value.hour,
                    value.minute,
                    value.second,
                )
            )

        return None

    def _collect_contract_records(
        self, state_changes: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        contracts: dict[str, dict[str, Any]] = {}

        for state_change in state_changes:
            if not isinstance(state_change, dict):
                continue
            key = state_change.get("key")
            if not isinstance(key, str) or "." not in key:
                continue

            contract_name, variable = key.split(".", 1)
            if variable not in {"__source__", "__code__", "__submitted__"}:
                continue

            record = contracts.setdefault(
                contract_name,
                {"source": None, "code": None, "submitted_at": None},
            )

            if variable == "__source__":
                record["source"] = state_change.get("value")
            elif variable == "__code__":
                record["code"] = state_change.get("value")
            else:
                record["submitted_at"] = self._submission_time_from_state_value(
                    state_change.get("value")
                )

        return contracts
