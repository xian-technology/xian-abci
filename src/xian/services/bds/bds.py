from __future__ import annotations

import ast
import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
from xian.services.bds.shielded import collect_shielded_output_tags

GENESIS_BLOCK_HASH = "GENESIS"
GENESIS_TX_HASH = "GENESIS"
GENESIS_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)
XSC001_REQUIRED_EXPORTS = {
    "change_metadata": ("key", "value"),
    "transfer": ("amount", "to"),
    "approve": ("amount", "to"),
    "transfer_from": ("amount", "to", "main_account"),
    "balance_of": ("address",),
}


def _jsonb_param(value: Any) -> str:
    return canonical_json_text(value)


def _nullable_jsonb_param(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_json_text(value)


def _normalize_json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return json.dumps(canonical_json_value(value), separators=(",", ":"))


def _normalize_token_balance(
    raw_value: Any,
    numeric_value: Decimal | None,
) -> str | None:
    if numeric_value is not None:
        return str(numeric_value)
    return _normalize_json_text(raw_value)


def _json_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _is_name_call(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _has_export_decorator(node: ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "export":
            return True
        if _is_name_call(decorator, "export"):
            return True
    return False


def _positional_arg_names(node: ast.FunctionDef) -> tuple[str, ...]:
    return tuple(arg.arg for arg in node.args.posonlyargs + node.args.args)


def _assigns_balances_hash(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        targets = node.targets
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value = node.value
    else:
        return False

    return (
        value is not None
        and _is_name_call(value, "Hash")
        and any(isinstance(target, ast.Name) and target.id == "balances" for target in targets)
    )


def _source_has_xsc001_surface(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    has_balances_hash = False
    exported_functions: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if _assigns_balances_hash(node):
            has_balances_hash = True
            continue
        if isinstance(node, ast.FunctionDef) and _has_export_decorator(node):
            exported_functions[node.name] = _positional_arg_names(node)

    if not has_balances_hash:
        return False

    return all(
        exported_functions.get(function_name) == required_args
        for function_name, required_args in XSC001_REQUIRED_EXPORTS.items()
    )


@dataclass(frozen=True, slots=True)
class SpoolFileInfo:
    path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SpoolStatusSnapshot:
    files: tuple[SpoolFileInfo, ...]
    total_bytes: int
    oldest_pending: dict[str, Any] | None
    newest_pending: dict[str, Any] | None


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

        self._worker_task = asyncio.create_task(self._run_worker(), name="xian-bds-worker")

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
                            self._indexed_height = int(payload.block_meta["height"])
                            self._prune_stale_pending_payloads()
                            break
                        await asyncio.sleep(self.WORKER_RETRY_DELAY_SECONDS)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception(f"BDS worker failed to persist block: {exc}")
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
                await asyncio.wait_for(self.flush(), timeout=self.CLOSE_FLUSH_TIMEOUT_SECONDS)
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
        return BdsBlockPayload.from_spool_dict(json.loads(spool_path.read_text(encoding="utf-8")))

    def _pending_spool_files(self) -> list[Path]:
        return sorted(self.spool_dir.glob("*.json"))

    def _pending_spool_file_infos(self) -> list[SpoolFileInfo]:
        pending: list[SpoolFileInfo] = []
        for path in self._pending_spool_files():
            try:
                pending.append(SpoolFileInfo(path=path, size_bytes=path.stat().st_size))
            except FileNotFoundError:
                continue
        return pending

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

    def _prune_stale_pending_payloads(self) -> None:
        if self._indexed_height is None or not self._pending_payloads:
            return
        stale_heights = [
            height for height in self._pending_payloads if height <= self._indexed_height
        ]
        for height in stale_heights:
            self._pending_payloads.pop(height, None)

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
                return int(self._read_spool_file(spool_path).block_meta["height"])
            except Exception:
                return None

    async def compact_spool(self) -> dict[str, Any]:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        indexed_height = await self.db.fetchval("SELECT MAX(height) FROM blocks")
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

    async def drain_spool(self, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
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
        self,
        spool_file: SpoolFileInfo,
        payload: BdsBlockPayload | None = None,
    ) -> dict[str, Any]:
        loaded_payload = payload or self._read_spool_file(spool_file.path)
        return {
            "file": spool_file.path.name,
            "size_bytes": spool_file.size_bytes,
            "block_height": int(loaded_payload.block_meta["height"]),
            "block_hash": str(loaded_payload.block_meta["hash"]),
            "block_time": utc_datetime(loaded_payload.block_time).isoformat(),
            "tx_count": len(loaded_payload.transactions),
            "state_patch_count": len(loaded_payload.state_patches),
            "app_hash": loaded_payload.app_hash,
        }

    def _spool_status_snapshot(self) -> SpoolStatusSnapshot:
        files = tuple(self._pending_spool_file_infos())
        return SpoolStatusSnapshot(
            files=files,
            total_bytes=sum(spool_file.size_bytes for spool_file in files),
            oldest_pending=(self._spool_entry_metadata(files[0]) if files else None),
            newest_pending=(self._spool_entry_metadata(files[-1]) if files else None),
        )

    async def get_status(self, *, current_block_height: int | None = None) -> dict[str, Any]:
        spool_snapshot = self._spool_status_snapshot()

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
        if isinstance(indexed_height, int):
            if self._indexed_height is None or indexed_height > self._indexed_height:
                self._indexed_height = indexed_height
        self._prune_stale_pending_payloads()
        height_lag = None
        if isinstance(current_block_height, int) and isinstance(indexed_height, int):
            height_lag = max(current_block_height - indexed_height, 0)

        queue_depth = len(self._pending_payloads)
        queue_capacity = max(self.config.queue_max_size, 1)
        disk_usage = shutil.disk_usage(self.spool_dir)

        pool_stats: dict[str, Any] | None = None
        if self.db.pool is not None:
            try:
                size = self.db.pool.get_size()
                idle = self.db.pool.get_idle_size()
                max_size = self.db.pool.get_max_size()
                min_size = self.db.pool.get_min_size()
                in_use = max(size - idle, 0)
                utilization = (in_use / max_size) if max_size > 0 else 0.0
                pool_stats = {
                    "size": size,
                    "idle": idle,
                    "in_use": in_use,
                    "max_size": max_size,
                    "min_size": min_size,
                    "utilization": utilization,
                }
            except Exception as exc:  # asyncpg API quirks — don't fail status
                logger.debug(f"Unable to collect pool stats: {exc}")

        alerts: list[dict[str, Any]] = []
        if db_status != "ok":
            alerts.append(
                {
                    "level": "error",
                    "code": "db_unavailable",
                    "message": "BDS database status is degraded",
                }
            )
        if len(spool_snapshot.files) >= self.config.spool_warn_entries:
            alerts.append(
                {
                    "level": "warning",
                    "code": "spool_entries_high",
                    "message": "BDS spool entry count exceeded warning threshold",
                    "threshold": self.config.spool_warn_entries,
                    "value": len(spool_snapshot.files),
                }
            )
        if spool_snapshot.total_bytes >= self.config.spool_warn_bytes:
            alerts.append(
                {
                    "level": "warning",
                    "code": "spool_bytes_high",
                    "message": "BDS spool size exceeded warning threshold",
                    "threshold": self.config.spool_warn_bytes,
                    "value": spool_snapshot.total_bytes,
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

        has_spool_backlog = len(spool_snapshot.files) > 0
        has_height_lag = isinstance(height_lag, int) and height_lag > 0

        return {
            "worker_running": self._worker_task is not None and not self._worker_task.done(),
            "catchup_running": self._catchup_task is not None and not self._catchup_task.done(),
            "queue_depth": queue_depth,
            "queue_capacity": queue_capacity,
            "queue_utilization": queue_depth / queue_capacity,
            "spool_dir": str(self.spool_dir),
            "spool_pending_count": len(spool_snapshot.files),
            "spool_total_bytes": spool_snapshot.total_bytes,
            "spool_oldest_pending": spool_snapshot.oldest_pending,
            "spool_newest_pending": spool_snapshot.newest_pending,
            "storage": {
                "filesystem_total_bytes": disk_usage.total,
                "filesystem_used_bytes": disk_usage.used,
                "filesystem_free_bytes": disk_usage.free,
            },
            "db_status": db_status,
            "db_error": db_error,
            "pool": pool_stats,
            "last_enqueue_error": self._last_enqueue_error,
            "indexed": indexed,
            "current_block_height": current_block_height,
            "height_lag": height_lag,
            "catching_up": has_spool_backlog or has_height_lag,
            "alerts": alerts,
        }

    async def _refresh_indexed_height(self) -> int | None:
        indexed_height = await self.db.fetchval("SELECT MAX(height) FROM blocks")
        self._indexed_height = int(indexed_height) if indexed_height is not None else None
        self._prune_stale_pending_payloads()
        return self._indexed_height

    async def _start_catchup(self) -> None:
        if not self.config.catchup_enabled or self._catchup_task is not None:
            return
        if not self.config.rpc_url:
            return
        await self._ensure_catchup_runtime()
        self._catchup_task = asyncio.create_task(self._run_catchup(), name="xian-bds-catchup")

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
        indexed_height = self._indexed_height if self._indexed_height is not None else 0
        highest_pending = max(self._pending_payloads, default=indexed_height)
        target_height = max(int(latest_height), int(highest_pending))
        next_height = indexed_height + 1

        while next_height <= target_height:
            if len(self._pending_payloads) >= max(self.config.queue_max_size, 1):
                return
            if next_height in self._pending_payloads:
                next_height += 1
                continue
            payload = await self._reindexer.build_payload(next_height)
            if not self._enqueue_pending_payload(payload):
                return
            next_height += 1

    async def get_spool_entries(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        pending_spool = self._pending_spool_file_infos()
        selected = pending_spool[offset : offset + limit]
        return [self._spool_entry_metadata(spool_file) for spool_file in selected]

    async def _prepare_schema(self) -> None:
        await self.db.execute(sql.create_meta())
        current_version = await self.db.fetchval(sql.select_schema_version())
        if current_version is None:
            if await self._managed_tables_present():
                raise RuntimeError(
                    "BDS schema metadata is missing for existing tables; "
                    "reset BDS storage before starting the current runtime"
                )
        elif current_version != str(sql.SCHEMA_VERSION):
            logger.warning(
                "Resetting BDS schema from version {} to {}",
                current_version,
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
            sql.create_shielded_output_tags(),
            sql.create_contracts(),
            sql.create_state_patches(),
        ):
            await self.db.execute(statement)

        await self.db.execute(
            sql.upsert_schema_version(),
            [str(sql.SCHEMA_VERSION), utc_datetime(datetime.now())],
        )

    async def _managed_tables_present(self) -> bool:
        return bool(
            await self.db.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                        'blocks',
                        'transactions',
                        'state',
                        'state_changes',
                        'events',
                        'rewards',
                        'contracts',
                        'state_patches',
                        'shielded_output_tags'
                      )
                );
                """
            )
        )

    async def process_genesis_block(self, cometbft_genesis: dict):
        start_time = timer()
        genesis_state = cometbft_genesis["abci_genesis"]["genesis"]
        block_time = GENESIS_CREATED_AT

        async with self.db.acquire() as connection:
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
                    _jsonb_param({"status": "ok", "genesis_entries": len(genesis_state)}),
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
                    previous_change_id, previous_tx_hash = current_index.get(key, (None, None))
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

                for contract_name, record in self._collect_contract_records(genesis_state).items():
                    display_source = record["source"]
                    if display_source is None:
                        continue
                    submission_time = record["submitted_at"] or self.get_submission_time(
                        genesis_state, contract_name
                    )
                    await connection.execute(
                        sql.upsert_contract(),
                        contract_name,
                        GENESIS_TX_HASH,
                        0,
                        submission_time,
                        display_source,
                        self.is_XSC001(display_source),
                    )

        logger.debug(f"Saved genesis block to BDS in {timer() - start_time:.3f} seconds")

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

            async with self.db.acquire() as connection:
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
                    touched_keys.update(patch["key"] for patch in state_patches if "key" in patch)
                    current_index = await self._load_current_state_index(connection, touched_keys)

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
            tx_result["chi_used"],
            _nullable_jsonb_param(canonical_result_value(tx_result.get("result"))),
            _jsonb_param(payload),
            _jsonb_param(tx.envelope),
            block_time,
        )

        for write_index, state_change in enumerate(tx_result["state"]):
            key = state_change["key"]
            value = _jsonb_param(state_change["value"])
            previous_change_id, previous_tx_hash = current_index.get(key, (None, None))
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

        kwargs = payload.get("kwargs")
        if isinstance(kwargs, dict):
            for row in collect_shielded_output_tags(
                contract=str(payload.get("contract", "")),
                function=str(payload.get("function", "")),
                tx_hash=tx_hash,
                block_height=int(block_meta["height"]),
                tx_index=tx_index,
                tx_result_events=tx_result.get("events", []),
                kwargs=kwargs,
            ):
                await connection.execute(
                    sql.insert_shielded_output_tag(),
                    row["block_height"],
                    row["tx_hash"],
                    row["tx_index"],
                    row["contract"],
                    row["function"],
                    row["action"],
                    row["output_index"],
                    row["note_index"],
                    row["commitment"],
                    row["new_root"],
                    row["payload_hash"],
                    row["tag_kind"],
                    row["tag_value"],
                    block_time,
                )

        if tx_result["status"] == 0:
            for contract_name, record in self._collect_contract_records(
                tx_result.get("state", [])
            ).items():
                display_source = record["source"]
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
                    self.is_XSC001(display_source),
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
                        "governance_contract": execution.get("governance_contract"),
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
            previous_change_id, previous_tx_hash = current_index.get(key, (None, None))
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
        return {row["key"]: (row["last_change_id"], row["last_tx_hash"]) for row in rows}

    async def get_contracts(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_contracts(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_contract_summary(self, contract_name: str):
        row = await self.db.fetchrow(sql.select_contract_summary(), [contract_name])
        return dict(row) if row is not None else None

    async def get_blocks(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_blocks(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_block(self, block_height: int):
        row = await self.db.fetchrow(sql.select_block_by_height(), [block_height])
        return dict(row) if row is not None else None

    async def get_block_by_hash(self, block_hash: str):
        row = await self.db.fetchrow(sql.select_block_by_hash(), [block_hash])
        return dict(row) if row is not None else None

    async def get_tx(self, tx_hash: str):
        row = await self.db.fetchrow(sql.select_transaction_by_hash(), [tx_hash])
        return dict(row) if row is not None else None

    async def get_txs_for_block(self, block_ref: str):
        if len(block_ref) == 64:
            rows = await self.db.fetch(sql.select_transactions_for_block_hash(), [block_ref])
        else:
            rows = await self.db.fetch(sql.select_transactions_for_block_height(), [int(block_ref)])
        return [dict(row) for row in rows]

    async def get_txs_by_sender(self, sender: str, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_transactions_by_sender(), [sender, limit, offset])
        return [dict(row) for row in rows]

    async def get_recent_addresses(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_recent_addresses(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_txs_by_contract(self, contract: str, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_transactions_by_contract(), [contract, limit, offset])
        return [dict(row) for row in rows]

    async def get_events_for_tx(self, tx_hash: str):
        rows = await self.db.fetch(sql.select_events_for_tx(), [tx_hash])
        return [dict(row) for row in rows]

    async def get_shielded_output_tags(
        self,
        tag_value: str,
        limit: int = 100,
        offset: int = 0,
        *,
        kind: str = "sync_hint",
        after_id: int | None = None,
    ):
        if after_id is not None:
            rows = await self.db.fetch(
                sql.select_shielded_output_tags_after_id(),
                [kind, tag_value, after_id, limit],
            )
        else:
            rows = await self.db.fetch(
                sql.select_shielded_output_tags(),
                [kind, tag_value, limit, offset],
            )
        return [dict(row) for row in rows]

    async def get_shielded_wallet_history(
        self,
        tag_value: str,
        limit: int = 100,
        after_note_index: int = 0,
        *,
        kind: str = "sync_hint",
    ):
        if limit <= 0:
            return []

        event_batch_size = max(1, min(limit, 1000))
        next_event_id = 0
        history_rows: list[dict[str, Any]] = []

        while len(history_rows) < limit:
            event_rows = await self.db.fetch(
                sql.select_events_by_event_after_id(),
                ["ShieldedOutputsCommitted", next_event_id, event_batch_size],
            )
            if not event_rows:
                break

            events = [dict(row) for row in event_rows]
            last_event = events[-1]
            if isinstance(last_event.get("id"), int):
                next_event_id = int(last_event["id"])

            output_rows: list[dict[str, Any]] = []
            tx_hashes: set[str] = set()
            for event in events:
                data = _json_mapping(event.get("data")) or {}
                note_index_start = data.get("note_index_start")
                output_count = data.get("output_count")
                commitments_blob = data.get("commitments_blob")
                if not isinstance(note_index_start, int) or not isinstance(commitments_blob, str):
                    continue
                commitments = [item for item in commitments_blob.split("|") if item != ""]
                resolved_output_count = (
                    output_count if isinstance(output_count, int) else len(commitments)
                )
                if len(commitments) < resolved_output_count:
                    continue

                tx_hash = event.get("tx_hash")
                if not isinstance(tx_hash, str):
                    continue
                tx_hashes.add(tx_hash)

                for output_index in range(resolved_output_count):
                    note_index = note_index_start + output_index
                    if note_index < after_note_index:
                        continue
                    output_rows.append(
                        {
                            "event_id": event.get("id"),
                            "tx_hash": tx_hash,
                            "block_height": event.get("block_height"),
                            "tx_index": event.get("tx_index"),
                            "contract": event.get("contract"),
                            "function": None,
                            "action": None,
                            "output_index": output_index,
                            "note_index": note_index,
                            "commitment": commitments[output_index],
                            "new_root": data.get("new_root"),
                            "payload_hash": None,
                            "tag_kind": None,
                            "tag_value": None,
                            "output_payload": None,
                            "created_at": event.get("created_at"),
                        }
                    )
                    if len(history_rows) + len(output_rows) >= limit:
                        break
                if len(history_rows) + len(output_rows) >= limit:
                    break

            if not output_rows:
                if len(events) < event_batch_size:
                    break
                continue

            payload_rows = await self.db.fetch(
                sql.select_transactions_payloads_for_hashes(),
                [sorted(tx_hashes)],
            )
            payload_items = [dict(row) for row in payload_rows]
            payloads_by_hash = {
                str(row["hash"]): _json_mapping(row.get("payload")) or {} for row in payload_items
            }

            tag_rows = await self.db.fetch(
                sql.select_shielded_output_tags_for_transactions(),
                [kind, tag_value, sorted(tx_hashes)],
            )
            tag_items = [dict(row) for row in tag_rows]
            matching_tags = {
                (str(row["tx_hash"]), int(row["output_index"])): dict(row)
                for row in tag_items
                if isinstance(row.get("tx_hash"), str) and isinstance(row.get("output_index"), int)
            }

            for row in output_rows:
                tx_hash = row["tx_hash"]
                output_index = row["output_index"]
                payload = payloads_by_hash.get(tx_hash, {})
                kwargs = _json_mapping(payload.get("kwargs")) or {}
                payloads = kwargs.get("output_payloads")
                if (
                    isinstance(payloads, list)
                    and output_index < len(payloads)
                    and isinstance(payloads[output_index], str)
                    and (tx_hash, output_index) in matching_tags
                ):
                    tag = matching_tags[(tx_hash, output_index)]
                    row["output_payload"] = payloads[output_index]
                    row["payload_hash"] = tag.get("payload_hash")
                    row["tag_kind"] = tag.get("tag_kind")
                    row["tag_value"] = tag.get("tag_value")

                function = payload.get("function")
                if isinstance(function, str):
                    row["function"] = function
                action = _json_mapping(payload.get("kwargs")) or {}
                resolved_action = action.get("action")
                if isinstance(resolved_action, str):
                    row["action"] = resolved_action

                history_rows.append(row)
                if len(history_rows) >= limit:
                    break

            if len(events) < event_batch_size:
                break

        return history_rows

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

    async def get_token_balances(
        self,
        address: str,
        limit: int = 100,
        offset: int = 0,
        *,
        include_zero: bool = False,
    ):
        rows = await self.db.fetch(
            sql.select_token_balances(),
            [address, include_zero, limit, offset],
        )
        items: list[dict[str, Any]] = []
        total = 0

        for row in rows:
            record = dict(row)
            total = int(record.pop("total_count", 0) or 0)
            items.append(
                {
                    "contract": record["contract"],
                    "balance": _normalize_token_balance(
                        record.pop("balance", None),
                        record.pop("balance_numeric", None),
                    ),
                    "last_tx_hash": record.get("last_tx_hash"),
                    "last_block_height": record.get("last_block_height"),
                    "updated_at": record.get("updated_at"),
                    "name": _normalize_json_text(record.get("token_name")),
                    "symbol": _normalize_json_text(record.get("token_symbol")),
                    "logo_url": _normalize_json_text(record.get("token_logo_url")),
                }
            )

        return {
            "available": True,
            "address": address,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get_state_history(self, key: str, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_state_history(), [key, limit, offset])
        return [dict(row) for row in rows]

    async def get_state_for_tx(self, tx_hash: str):
        rows = await self.db.fetch(sql.select_state_tx(), [tx_hash])
        return [dict(row) for row in rows]

    async def get_state_for_block(self, key: str):
        if len(key) == 64:
            rows = await self.db.fetch(sql.select_state_block_hash(), [key])
        else:
            rows = await self.db.fetch(sql.select_state_block_height(), [int(key)])
        return [dict(row) for row in rows]

    async def get_state_patches(self, limit: int = 100, offset: int = 0):
        rows = await self.db.fetch(sql.select_state_patches(), [limit, offset])
        return [dict(row) for row in rows]

    async def get_state_patches_for_block(self, block_height: int):
        rows = await self.db.fetch(sql.select_state_patches_for_block(), [block_height])
        return [dict(row) for row in rows]

    async def get_state_patch_by_hash(self, patch_hash: str):
        row = await self.db.fetchrow(sql.select_state_patch_by_hash(), [patch_hash])
        return dict(row) if row is not None else None

    async def get_state_changes_for_patch(self, patch_hash: str):
        rows = await self.db.fetch(sql.select_state_changes_for_patch(), [patch_hash])
        return [dict(row) for row in rows]

    async def get_developer_rewards(self, recipient_key: str):
        row = await self.db.fetchrow(sql.select_developer_rewards_summary(), [recipient_key])
        return dict(row) if row is not None else None

    def is_XSC001(self, source: str) -> bool:
        return _source_has_xsc001_surface(source)

    def get_submission_time(
        self, genesis_state: list[dict[str, Any]], contract_name: str
    ) -> datetime:
        for item in genesis_state:
            if "con_" not in contract_name:
                if contract_name == "submission":
                    return GENESIS_CREATED_AT
                return datetime(1970, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            if isinstance(item, dict) and item.get("key") == f"{contract_name}.__submitted__":
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
            if variable not in {"__source__", "__submitted__"}:
                continue

            record = contracts.setdefault(contract_name, {"source": None, "submitted_at": None})

            if variable == "__source__":
                record["source"] = state_change.get("value")
            else:
                record["submitted_at"] = self._submission_time_from_state_value(
                    state_change.get("value")
                )

        return contracts
