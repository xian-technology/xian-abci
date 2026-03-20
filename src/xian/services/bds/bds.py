from __future__ import annotations

from datetime import datetime, timezone
from timeit import default_timer as timer
from typing import Any

from loguru import logger
from xian_py.decompiler import ContractDecompiler

from xian.services.bds import sql
from xian.services.bds.config import BdsConfig
from xian.services.bds.database import DB
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
    def __init__(self, config: BdsConfig):
        self.config = config
        self.db = DB(config)

    async def init(self, cometbft_genesis: dict):
        await self.db.init_pool()
        await self._prepare_schema()

        if not await self.db.has_entries("blocks"):
            await self.process_genesis_block(cometbft_genesis)

        logger.info("BDS service initialized")
        return self

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

    async def persist_block(
        self,
        *,
        block_meta: dict[str, Any],
        block_time: datetime,
        app_hash: str,
        transactions: list[dict[str, Any]],
        state_patches: list[dict[str, Any]] | None = None,
        state_patch_hash: str | None = None,
    ) -> None:
        start_time = timer()
        created_at = utc_datetime(block_time)
        state_patches = state_patches or []

        try:
            async with self.db.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        sql.insert_block(),
                        block_meta["height"],
                        block_meta["hash"],
                        block_meta["nanos"],
                        created_at,
                        len(transactions),
                        app_hash,
                        created_at,
                    )

                    touched_keys = {
                        state_change["key"]
                        for tx in transactions
                        for state_change in tx["tx_result"]["state"]
                    }
                    touched_keys.update(
                        patch["key"]
                        for patch in state_patches
                        if "key" in patch
                    )
                    current_index = await self._load_current_state_index(
                        connection, touched_keys
                    )

                    for tx in transactions:
                        await self._persist_transaction(
                            connection,
                            block_meta=block_meta,
                            block_time=created_at,
                            current_index=current_index,
                            tx=tx,
                        )

                    if state_patches:
                        await self._persist_state_patches(
                            connection,
                            block_meta=block_meta,
                            block_time=created_at,
                            current_index=current_index,
                            state_patches=state_patches,
                            state_patch_hash=state_patch_hash
                            or f"STATE_PATCH_{block_meta['height']}",
                            tx_index=len(transactions),
                        )
        except Exception as exc:
            logger.exception(f"Failed to persist block to BDS: {exc}")
            return

        logger.debug(
            "Saved block {} to BDS in {:.3f} seconds",
            block_meta["height"],
            timer() - start_time,
        )

    async def _persist_transaction(
        self,
        connection,
        *,
        block_meta: dict[str, Any],
        block_time: datetime,
        current_index: dict[str, tuple[int | None, str | None]],
        tx: dict[str, Any],
    ) -> None:
        tx_result = tx["tx_result"]
        payload = tx["payload"]
        tx_hash = tx_result["hash"]
        tx_index = int(tx["tx_index"])

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
            canonical_json_value(tx["envelope"]),
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
