from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from timeit import default_timer as timer
from typing import Any

from loguru import logger
from xian_py.decompiler import ContractDecompiler

from xian.services.bds import sql
from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload
from xian.services.bds.serializer import (
    canonical_decimal,
    canonical_json_value,
    canonical_result_value,
    utc_datetime,
)

GENESIS_BLOCK_HASH = "GENESIS"
GENESIS_TX_HASH = "GENESIS"
GENESIS_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


class BDS:
    WORKER_RETRY_DELAY_SECONDS = 2.0
    CLOSE_FLUSH_TIMEOUT_SECONDS = 1.0

    def __init__(self, config: BdsConfig):
        self.config = config
        self.db = DB(config)
        self.spool_dir = Path(config.spool_dir or ".bds-spool")
        self._queue: (
            asyncio.Queue[tuple[BdsBlockPayload, Path] | None] | None
        ) = None
        self._worker_task: asyncio.Task | None = None

    async def initialize_storage(
        self,
        cometbft_genesis: dict,
        *,
        reset: bool = False,
    ) -> None:
        await self.db.init_pool()
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        if reset:
            self.clear_spool()
            await self.db.execute(sql.drop_all_tables())
        await self._prepare_schema()

        if not await self.db.has_entries("blocks"):
            await self.process_genesis_block(cometbft_genesis)

    async def init(self, cometbft_genesis: dict):
        await self.initialize_storage(cometbft_genesis)
        self._start_worker()
        await self._replay_spool()
        logger.info("BDS service initialized")
        return self

    def _start_worker(self) -> None:
        if self._queue is not None and self._worker_task is not None:
            return

        self._queue = asyncio.Queue(maxsize=max(self.config.queue_max_size, 1))
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="xian-bds-worker"
        )

    async def _run_worker(self) -> None:
        assert self._queue is not None

        while True:
            queued = await self._queue.get()
            try:
                if queued is None:
                    return
                payload, spool_path = queued
                while True:
                    try:
                        if await self.persist_block(payload):
                            if spool_path.exists():
                                spool_path.unlink()
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
                self._queue.task_done()

    async def enqueue_block(self, payload: BdsBlockPayload) -> None:
        if self._queue is None:
            raise RuntimeError("BDS worker is not initialized")
        spool_path = self._write_spool_file(payload)
        await self._queue.put((payload, spool_path))

    async def _replay_spool(self) -> None:
        if self._queue is None:
            return
        for spool_path in self._pending_spool_files():
            payload = self._read_spool_file(spool_path)
            await self._queue.put((payload, spool_path))

    async def flush(self) -> None:
        if self._queue is None:
            return
        await self._queue.join()

    async def close(self) -> None:
        if self._queue is not None:
            try:
                await asyncio.wait_for(
                    self.flush(), timeout=self.CLOSE_FLUSH_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for BDS queue flush; pending blocks remain in {}",
                    self.spool_dir,
                )
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None
        self._queue = None
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

        queue_depth = self._queue.qsize() if self._queue is not None else 0
        queue_capacity = max(self.config.queue_max_size, 1)

        return {
            "worker_running": self._worker_task is not None
            and not self._worker_task.done(),
            "queue_depth": queue_depth,
            "queue_capacity": queue_capacity,
            "queue_utilization": queue_depth / queue_capacity,
            "spool_dir": str(self.spool_dir),
            "spool_pending_count": len(pending_spool),
            "spool_oldest_pending": oldest_pending,
            "spool_newest_pending": newest_pending,
            "db_status": db_status,
            "db_error": db_error,
            "indexed": indexed,
            "current_block_height": current_block_height,
            "height_lag": height_lag,
            "catching_up": bool(pending_spool)
            or (isinstance(height_lag, int) and height_lag > 0),
        }

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
                    {"status": "ok", "genesis_entries": len(genesis_state)},
                    {"kind": "genesis"},
                    {
                        "kind": "genesis",
                        "genesis": canonical_json_value(genesis_state),
                    },
                    block_time,
                )

                current_index: dict[str, tuple[int | None, str | None]] = {}
                for write_index, state in enumerate(genesis_state):
                    key = state["key"]
                    value = canonical_json_value(state["value"])
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

                    if key.endswith(".__code__"):
                        contract_name = key.split(".", 1)[0]
                        original_code = ContractDecompiler().decompile(
                            state["value"]
                        )
                        submission_time = self.get_submission_time(
                            genesis_state, contract_name
                        )
                        await connection.execute(
                            sql.upsert_contract(),
                            contract_name,
                            GENESIS_TX_HASH,
                            0,
                            submission_time,
                            original_code,
                            self.is_XSC0001(original_code),
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
            canonical_result_value(tx_result.get("result")),
            canonical_json_value(payload),
            canonical_json_value(tx.envelope),
            block_time,
        )

        for write_index, state_change in enumerate(tx_result["state"]):
            key = state_change["key"]
            value = canonical_json_value(state_change["value"])
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
        for reward_type, rewards in tx_result.get("rewards", {}).items():
            for recipient_key, value in rewards.items():
                await connection.execute(
                    sql.insert_reward(),
                    block_meta["height"],
                    tx_hash,
                    tx_index,
                    reward_index,
                    reward_type,
                    recipient_key,
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
                canonical_json_value(event.get("data_indexed", {})),
                canonical_json_value(event.get("data", {})),
                block_time,
            )

        if (
            tx_result["status"] == 0
            and payload["contract"] == "submission"
            and payload["function"] == "submit_contract"
        ):
            code = payload["kwargs"]["code"]
            await connection.execute(
                sql.upsert_contract(),
                payload["kwargs"]["name"],
                tx_hash,
                block_meta["height"],
                block_time,
                code,
                self.is_XSC0001(code),
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
            {
                "patch_count": len(state_patches),
                "comment": "State Patch Pseudo-Transaction",
            },
            {"kind": "state_patch"},
            {
                "kind": "state_patch",
                "patches": canonical_json_value(
                    [
                        {
                            "key": patch["key"],
                            "value": patch["value"],
                            "comment": patch.get("comment", ""),
                        }
                        for patch in state_patches
                    ]
                ),
            },
            block_time,
        )

        for write_index, patch in enumerate(state_patches):
            key = patch["key"]
            value = canonical_json_value(patch["value"])
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
            len(state_patches),
            canonical_json_value(
                [
                    {
                        "key": patch["key"],
                        "value": patch["value"],
                        "comment": patch.get("comment", ""),
                    }
                    for patch in state_patches
                ]
            ),
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
        self, contract: str, event: str, limit: int = 100, offset: int = 0
    ):
        rows = await self.db.fetch(
            sql.select_events_by_contract_event(),
            [contract, event, limit, offset],
        )
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
