from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from xian.services.bds import sql
from xian.services.bds.bds import BDS
from xian.services.bds.reindex import RpcBlockSource

SNAPSHOT_FORMAT_VERSION = 1
IMPORT_BATCH_SIZE = 1000


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    order_by: str
    datetime_columns: frozenset[str] = frozenset()
    decimal_columns: frozenset[str] = frozenset()
    overriding_system_value: bool = False
    import_query: str | None = None

    @property
    def select_query(self) -> str:
        columns = ", ".join(self.columns)
        return f"SELECT {columns} FROM {self.name} ORDER BY {self.order_by};"

    @property
    def insert_query(self) -> str:
        if self.import_query is not None:
            return self.import_query
        columns = ", ".join(self.columns)
        placeholders = ", ".join(
            f"${index}" for index in range(1, len(self.columns) + 1)
        )
        overriding = (
            " OVERRIDING SYSTEM VALUE" if self.overriding_system_value else ""
        )
        return (
            f"INSERT INTO {self.name} ({columns}){overriding} "
            f"VALUES ({placeholders});"
        )


TABLE_SPECS = (
    TableSpec(
        name="bds_meta",
        columns=("key", "value", "updated_at"),
        order_by="key ASC",
        datetime_columns=frozenset({"updated_at"}),
        import_query="""
        INSERT INTO bds_meta (key, value, updated_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = EXCLUDED.updated_at;
        """.strip(),
    ),
    TableSpec(
        name="blocks",
        columns=(
            "height",
            "block_hash",
            "block_time",
            "block_time_iso",
            "tx_count",
            "app_hash",
            "created_at",
        ),
        order_by="height ASC",
        datetime_columns=frozenset({"block_time_iso", "created_at"}),
    ),
    TableSpec(
        name="transactions",
        columns=(
            "hash",
            "block_height",
            "block_hash",
            "block_time",
            "tx_index",
            "sender",
            "nonce",
            "contract",
            "function",
            "success",
            "status_code",
            "chi_used",
            "result",
            "payload",
            "envelope",
            "created_at",
        ),
        order_by="block_height ASC, tx_index ASC",
        datetime_columns=frozenset({"created_at"}),
    ),
    TableSpec(
        name="state_changes",
        columns=(
            "change_id",
            "block_height",
            "block_hash",
            "block_time",
            "tx_hash",
            "tx_index",
            "write_index",
            "key",
            "new_value",
            "previous_change_id",
            "previous_tx_hash",
            "origin_type",
            "created_at",
        ),
        order_by="change_id ASC",
        datetime_columns=frozenset({"created_at"}),
        overriding_system_value=True,
    ),
    TableSpec(
        name="state",
        columns=(
            "key",
            "value",
            "last_change_id",
            "last_tx_hash",
            "last_block_height",
            "updated_at",
        ),
        order_by="key ASC",
        datetime_columns=frozenset({"updated_at"}),
    ),
    TableSpec(
        name="events",
        columns=(
            "id",
            "block_height",
            "tx_hash",
            "tx_index",
            "event_index",
            "contract",
            "event",
            "signer",
            "caller",
            "data_indexed",
            "data",
            "created_at",
        ),
        order_by="id ASC",
        datetime_columns=frozenset({"created_at"}),
        overriding_system_value=True,
    ),
    TableSpec(
        name="rewards",
        columns=(
            "id",
            "block_height",
            "tx_hash",
            "tx_index",
            "reward_index",
            "type",
            "recipient_key",
            "source_contract",
            "value",
            "created_at",
        ),
        order_by="id ASC",
        datetime_columns=frozenset({"created_at"}),
        decimal_columns=frozenset({"value"}),
        overriding_system_value=True,
    ),
    TableSpec(
        name="shielded_output_tags",
        columns=(
            "id",
            "block_height",
            "tx_hash",
            "tx_index",
            "contract",
            "function",
            "action",
            "output_index",
            "note_index",
            "commitment",
            "new_root",
            "payload_hash",
            "discovery_tag",
            "created_at",
        ),
        order_by="id ASC",
        datetime_columns=frozenset({"created_at"}),
        overriding_system_value=True,
    ),
    TableSpec(
        name="contracts",
        columns=(
            "name",
            "last_tx_hash",
            "submitted_at_block",
            "submitted_at",
            "code",
            "xsc0001",
        ),
        order_by="name ASC",
        datetime_columns=frozenset({"submitted_at"}),
    ),
    TableSpec(
        name="state_patches",
        columns=(
            "hash",
            "block_height",
            "block_hash",
            "block_time",
            "patch_count",
            "patches",
            "created_at",
        ),
        order_by="block_height ASC",
        datetime_columns=frozenset({"created_at"}),
    ),
)


def default_snapshot_output_path(
    *,
    output_dir: Path,
    indexed_height: int | None,
    now: datetime | None = None,
) -> Path:
    resolved_now = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    height_suffix = indexed_height if indexed_height is not None else "empty"
    return (
        output_dir / f"xian-bds-snapshot-h{height_suffix}-{resolved_now}.tar.gz"
    )


def _encode_row_value(spec: TableSpec, column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in spec.datetime_columns:
        return value.isoformat()
    if column in spec.decimal_columns:
        return str(value)
    return value


def _decode_row_value(spec: TableSpec, column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in spec.datetime_columns:
        return datetime.fromisoformat(str(value))
    if column in spec.decimal_columns:
        return Decimal(str(value))
    return value


def serialize_snapshot_row(
    spec: TableSpec, row: dict[str, Any]
) -> dict[str, Any]:
    return {
        column: _encode_row_value(spec, column, row[column])
        for column in spec.columns
    }


def deserialize_snapshot_row(
    spec: TableSpec, payload: dict[str, Any]
) -> tuple[Any, ...]:
    return tuple(
        _decode_row_value(spec, column, payload.get(column))
        for column in spec.columns
    )


def _safe_extract_snapshot(archive: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in archive.getmembers():
        member_path = (destination / member.name).resolve()
        if (
            destination_resolved not in member_path.parents
            and member_path != destination_resolved
        ):
            raise ValueError(f"unsafe snapshot member path: {member.name}")
    archive.extractall(path=destination, filter="data")


async def export_bds_snapshot(
    *,
    bds: BDS,
    output_path: Path,
    force: bool = False,
) -> dict[str, Any]:
    if output_path.exists() and not force:
        raise FileExistsError(f"snapshot already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    status = await bds.get_status()

    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        metadata: dict[str, Any] = {
            "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
            "schema_version": sql.SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "indexed": status["indexed"],
            "spool_pending_count": status["spool_pending_count"],
            "tables": {},
        }

        async with bds.db.acquire() as connection:
            async with connection.transaction():
                for spec in TABLE_SPECS:
                    row_count = 0
                    table_path = temp_root / f"{spec.name}.jsonl"
                    with table_path.open("w", encoding="utf-8") as handle:
                        async for record in connection.cursor(
                            spec.select_query
                        ):
                            serialized = serialize_snapshot_row(
                                spec, dict(record)
                            )
                            handle.write(
                                json.dumps(
                                    serialized,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                )
                            )
                            handle.write("\n")
                            row_count += 1
                    metadata["tables"][spec.name] = {"row_count": row_count}

        metadata_path = temp_root / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        with tarfile.open(output_path, "w:gz") as archive:
            archive.add(metadata_path, arcname="metadata.json")
            for spec in TABLE_SPECS:
                archive.add(
                    temp_root / f"{spec.name}.jsonl",
                    arcname=f"{spec.name}.jsonl",
                )

    return {
        "output_path": str(output_path),
        "indexed_height": status["indexed"]["indexed_height"],
        "spool_pending_count": status["spool_pending_count"],
        "tables": metadata["tables"],
    }


async def import_bds_snapshot(
    *,
    bds: BDS,
    snapshot_path: Path,
    clear_spool: bool = False,
    trusted_block_source: RpcBlockSource | None = None,
) -> dict[str, Any]:
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")

    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        with tarfile.open(snapshot_path, "r:gz") as archive:
            _safe_extract_snapshot(archive, temp_root)

        metadata = json.loads(
            (temp_root / "metadata.json").read_text(encoding="utf-8")
        )
        if metadata.get("snapshot_format_version") != SNAPSHOT_FORMAT_VERSION:
            raise ValueError("unsupported BDS snapshot format version")
        if metadata.get("schema_version") != sql.SCHEMA_VERSION:
            raise ValueError("BDS snapshot schema version mismatch")
        await _verify_snapshot_indexed_chain_state(
            metadata=metadata,
            trusted_block_source=trusted_block_source,
        )

        await bds.reset_schema()
        if clear_spool:
            bds.clear_spool()

        async with bds.db.acquire() as connection:
            async with connection.transaction():
                for spec in TABLE_SPECS:
                    table_path = temp_root / f"{spec.name}.jsonl"
                    if not table_path.exists():
                        raise FileNotFoundError(
                            f"snapshot table file missing: {table_path.name}"
                        )
                    batch: list[tuple[Any, ...]] = []
                    with table_path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            line = line.strip()
                            if not line:
                                continue
                            batch.append(
                                deserialize_snapshot_row(spec, json.loads(line))
                            )
                            if len(batch) >= IMPORT_BATCH_SIZE:
                                await connection.executemany(
                                    spec.insert_query, batch
                                )
                                batch.clear()
                    if batch:
                        await connection.executemany(spec.insert_query, batch)

                await connection.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('state_changes', 'change_id'),
                        COALESCE((SELECT MAX(change_id) FROM state_changes), 1),
                        true
                    );
                    """
                )
                await connection.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('events', 'id'),
                        COALESCE((SELECT MAX(id) FROM events), 1),
                        true
                    );
                    """
                )
                await connection.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('rewards', 'id'),
                        COALESCE((SELECT MAX(id) FROM rewards), 1),
                        true
                    );
                    """
                )

    compacted = await bds.compact_spool()
    status = await bds.get_status()
    return {
        "snapshot_path": str(snapshot_path),
        "indexed_height": status["indexed"]["indexed_height"],
        "tables": metadata.get("tables", {}),
        "clear_spool": clear_spool,
        "compacted": compacted,
        "status": status,
    }


async def _verify_snapshot_indexed_chain_state(
    *,
    metadata: dict[str, Any],
    trusted_block_source: RpcBlockSource | None,
) -> None:
    indexed = metadata.get("indexed")
    if not isinstance(indexed, dict):
        raise ValueError("BDS snapshot indexed metadata missing")

    indexed_height = indexed.get("indexed_height")
    if indexed_height is None:
        return

    indexed_block_hash = indexed.get("indexed_block_hash")
    indexed_app_hash = indexed.get("indexed_app_hash")
    if not isinstance(indexed_block_hash, str) or indexed_block_hash == "":
        raise ValueError("BDS snapshot indexed block hash missing")
    if not isinstance(indexed_app_hash, str) or indexed_app_hash == "":
        raise ValueError("BDS snapshot indexed app hash missing")

    if trusted_block_source is None:
        return

    trusted_block = await trusted_block_source.block(int(indexed_height))
    trusted_block_hash = str(trusted_block["block_id"]["hash"]).upper()
    trusted_app_hash = str(
        trusted_block["block"]["header"]["app_hash"]
    ).upper()
    if trusted_block_hash != str(indexed_block_hash).upper():
        raise ValueError("BDS snapshot indexed block hash mismatch")
    if trusted_app_hash != str(indexed_app_hash).upper():
        raise ValueError("BDS snapshot indexed app hash mismatch")
