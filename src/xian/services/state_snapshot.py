"""Offline application snapshot archive and catalog management."""

from __future__ import annotations

import hashlib
import json
import math
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from loguru import logger

from xian.execution_policy import current_execution_policy, validate_execution_policy
from xian.log_safety import error_fields
from xian.services.snapshot_limits import SnapshotArchiveReader, SnapshotLimits
from xian.state_export import (
    export_state,
    import_state,
    load_exported_state,
    prepare_state_import,
)

SNAPSHOT_FORMAT_VERSION = 2
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
SNAPSHOT_METADATA_FILENAME = "metadata.json"
SNAPSHOT_STATE_FILENAME = "exported_state.json"
SNAPSHOT_ARCHIVE_FILES = frozenset({SNAPSHOT_METADATA_FILENAME, SNAPSHOT_STATE_FILENAME})


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_snapshot(
    archive: tarfile.TarFile, destination: Path, *, limits: SnapshotLimits | None = None
) -> None:
    limits = limits or SnapshotLimits()
    names = set()
    for member in archive:
        _validate_snapshot_member(member, names)
        limits.validate_member_size(member.name, member.size)
        names.add(member.name)
    if names != SNAPSHOT_ARCHIVE_FILES:
        raise ValueError("snapshot archive must contain metadata.json and exported_state.json")
    limits.require_disk_space(destination, sum(member.size for member in archive.getmembers()))
    if isinstance(archive.fileobj, SnapshotArchiveReader):
        archive.fileobj.allow_payload_reads()
    archive.extractall(path=destination, filter="data")


def _validate_snapshot_member(member: tarfile.TarInfo, names: set[str]) -> None:
    if member.name not in SNAPSHOT_ARCHIVE_FILES or not member.isfile() or member.name in names:
        raise ValueError(f"unexpected, duplicate or non-file snapshot member: {member.name}")


def _validate_expected_checkpoint(
    exported_state: dict[str, Any],
    *,
    height: int | None,
    app_hash_hex: str | None,
) -> None:
    if height is not None and int(exported_state["number"]) != height:
        raise ValueError("snapshot height does not match expected checkpoint")
    if app_hash_hex is not None and exported_state["hash"] != app_hash_hex:
        raise ValueError("snapshot app hash does not match expected checkpoint")


def _validate_archive_metadata(metadata: dict, exported_state: dict, chain_id: str) -> None:
    validate_execution_policy(metadata.get("execution_policy"))
    if int(metadata["snapshot_format_version"]) != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot format version")
    if str(metadata["chain_id"]) != chain_id:
        raise ValueError("snapshot chain_id mismatch")
    if int(metadata["height"]) != int(exported_state["number"]):
        raise ValueError("snapshot height metadata mismatch")
    if str(metadata["app_hash"]) != str(exported_state["hash"]):
        raise ValueError("snapshot app hash metadata mismatch")


def default_snapshot_output_path(
    *,
    output_dir: Path,
    height: int,
    app_hash: str,
    now: datetime | None = None,
) -> Path:
    resolved_now = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    short_hash = app_hash[:12] if app_hash else "empty"
    return output_dir / (f"xian-state-snapshot-h{height}-{short_hash}-{resolved_now}.tar.gz")


@dataclass(frozen=True)
class SnapshotRecord:
    path: Path
    manifest_path: Path
    height: int
    format: int
    chunks: int
    app_hash_hex: str
    snapshot_hash_hex: str
    chunk_size: int
    metadata_bytes: bytes
    created_at: str


class StateSnapshotManager:
    def __init__(
        self,
        *,
        storage_home: Path,
        chain_id: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        limits: SnapshotLimits | None = None,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.chain_id = chain_id
        self.chunk_size = chunk_size
        self.limits = limits or SnapshotLimits()
        self.snapshot_dir = self.storage_home / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, *, storage_home: Path, chain_id: str, config: dict):
        return cls(
            storage_home=storage_home,
            chain_id=chain_id,
            limits=SnapshotLimits.from_config(config),
        )

    def _manifest_path(self, archive_path: Path) -> Path:
        return archive_path.parent / f"{archive_path.name}.manifest.json"

    def _cleanup_existing_height(
        self, *, height: int, format_version: int, keep_path: Path
    ) -> None:
        for record in self.list_snapshot_records():
            if record.height != height or record.format != format_version:
                continue
            if record.path.resolve() == keep_path.resolve():
                continue
            logger.info(
                "Removing previous snapshot for height={} format={}: {}",
                height,
                format_version,
                record.path,
            )
            record.path.unlink(missing_ok=True)
            record.manifest_path.unlink(missing_ok=True)

    def _build_manifest_metadata(
        self,
        *,
        height: int,
        app_hash_hex: str,
        exported_state_sha256: str,
        archive_sha256: str,
    ) -> bytes:
        return _json_bytes(
            {
                "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
                "execution_policy": current_execution_policy(),
                "chain_id": self.chain_id,
                "height": height,
                "app_hash": app_hash_hex,
                "chunk_size": self.chunk_size,
                "archive_sha256": archive_sha256,
                "exported_state_sha256": exported_state_sha256,
            }
        )

    def _write_manifest(
        self,
        *,
        archive_path: Path,
        height: int,
        app_hash_hex: str,
        archive_sha256: str,
        exported_state_sha256: str,
    ) -> SnapshotRecord:
        archive_size = archive_path.stat().st_size
        chunks = max(1, math.ceil(archive_size / self.chunk_size))
        metadata_bytes = self._build_manifest_metadata(
            height=height,
            app_hash_hex=app_hash_hex,
            exported_state_sha256=exported_state_sha256,
            archive_sha256=archive_sha256,
        )
        created_at = datetime.now(UTC).isoformat()
        manifest_payload = {
            "archive_name": archive_path.name,
            "height": height,
            "format": SNAPSHOT_FORMAT_VERSION,
            "chunks": chunks,
            "app_hash_hex": app_hash_hex,
            "snapshot_hash_hex": archive_sha256,
            "chunk_size": self.chunk_size,
            "metadata": json.loads(metadata_bytes.decode("utf-8")),
            "created_at": created_at,
        }
        manifest_path = self._manifest_path(archive_path)
        with TemporaryDirectory(dir=manifest_path.parent) as manifest_dir:
            staged_manifest = Path(manifest_dir) / manifest_path.name
            staged_manifest.write_text(
                json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            staged_manifest.replace(manifest_path)
        return SnapshotRecord(
            path=archive_path,
            manifest_path=manifest_path,
            height=height,
            format=SNAPSHOT_FORMAT_VERSION,
            chunks=chunks,
            app_hash_hex=app_hash_hex,
            snapshot_hash_hex=archive_sha256,
            chunk_size=self.chunk_size,
            metadata_bytes=metadata_bytes,
            created_at=created_at,
        )

    def list_snapshot_records(self) -> list[SnapshotRecord]:
        records: list[SnapshotRecord] = []
        for manifest_path in sorted(
            self.snapshot_dir.glob("*.tar.gz.manifest.json"),
            reverse=True,
        ):
            payload = json.loads(
                self.limits.read_bytes(
                    manifest_path, maximum=self.limits.max_metadata_bytes, label="manifest"
                )
            )
            archive_path = manifest_path.with_name(payload["archive_name"])
            if not archive_path.exists():
                logger.warning(
                    "Ignoring snapshot manifest without archive: {}",
                    manifest_path,
                )
                continue
            records.append(
                SnapshotRecord(
                    path=archive_path,
                    manifest_path=manifest_path,
                    height=int(payload["height"]),
                    format=int(payload["format"]),
                    chunks=int(payload["chunks"]),
                    app_hash_hex=str(payload["app_hash_hex"]),
                    snapshot_hash_hex=str(payload["snapshot_hash_hex"]),
                    chunk_size=int(payload["chunk_size"]),
                    metadata_bytes=_json_bytes(payload["metadata"]),
                    created_at=str(payload["created_at"]),
                )
            )
        records.sort(key=lambda record: (record.height, record.created_at), reverse=True)
        return records

    def export_snapshot(
        self,
        *,
        output_path: Path | None = None,
        force: bool = False,
        founder_private_key: str | None = None,
    ) -> dict[str, Any]:
        archive_path: Path | None = None
        exported_state_sha256: str | None = None
        height: int | None = None
        app_hash_hex: str | None = None
        with TemporaryDirectory(dir=self.snapshot_dir) as temp_dir:
            temp_root = Path(temp_dir)
            exported_state_path = export_state(
                output_dir=temp_root,
                founder_private_key=founder_private_key,
                storage_home=self.storage_home,
                output_filename=SNAPSHOT_STATE_FILENAME,
            )
            self.limits.check_size(
                exported_state_path.stat().st_size, self.limits.max_state_bytes, "exported state"
            )
            exported_state = load_exported_state(exported_state_path)
            height = int(exported_state["number"])
            app_hash_hex = str(exported_state["hash"])
            exported_state_sha256 = _sha256_file(exported_state_path)
            archive_path = (
                Path(output_path).expanduser().resolve()
                if output_path is not None
                else default_snapshot_output_path(
                    output_dir=self.snapshot_dir,
                    height=height,
                    app_hash=app_hash_hex,
                )
            )
            if archive_path.exists() and not force:
                raise FileExistsError(f"snapshot already exists: {archive_path}")

            metadata_path = temp_root / SNAPSHOT_METADATA_FILENAME
            metadata_path.write_bytes(
                _json_bytes(
                    {
                        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
                        "execution_policy": current_execution_policy(),
                        "chain_id": self.chain_id,
                        "height": height,
                        "app_hash": app_hash_hex,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
            )

            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=archive_path.parent) as archive_dir:
                staged_archive = Path(archive_dir) / archive_path.name
                with tarfile.open(staged_archive, "w:gz") as archive:
                    archive.add(metadata_path, arcname=SNAPSHOT_METADATA_FILENAME)
                    archive.add(exported_state_path, arcname=SNAPSHOT_STATE_FILENAME)
                self.limits.check_size(
                    staged_archive.stat().st_size, self.limits.max_archive_bytes, "archive"
                )
                staged_archive.replace(archive_path)

        assert archive_path is not None
        assert exported_state_sha256 is not None
        assert height is not None
        assert app_hash_hex is not None
        archive_sha256 = _sha256_file(archive_path)
        record = self._write_manifest(
            archive_path=archive_path,
            height=height,
            app_hash_hex=app_hash_hex,
            archive_sha256=archive_sha256,
            exported_state_sha256=exported_state_sha256,
        )
        self._cleanup_existing_height(
            height=height, format_version=SNAPSHOT_FORMAT_VERSION, keep_path=record.path
        )
        return {
            "output_path": str(record.path),
            "height": record.height,
            "app_hash": record.app_hash_hex,
            "format": record.format,
            "chunks": record.chunks,
        }

    def import_snapshot_archive(
        self,
        snapshot_path: Path,
        *,
        expected_height: int | None = None,
        expected_app_hash_hex: str | None = None,
    ) -> dict[str, Any]:
        if not snapshot_path.exists():
            raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
        self.limits.check_size(
            snapshot_path.stat().st_size, self.limits.max_archive_bytes, "archive"
        )
        with TemporaryDirectory(dir=self.snapshot_dir) as temp_dir:
            temp_root = Path(temp_dir)
            staged_archive = temp_root / "snapshot.tar.gz"
            self.limits.stage_archive(snapshot_path, staged_archive)
            unpacked_archive = temp_root / "snapshot.tar"
            self.limits.unpack_archive(staged_archive, unpacked_archive)
            extract_dir = temp_root / "extracted"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with unpacked_archive.open("rb") as file:
                with tarfile.open(
                    fileobj=SnapshotArchiveReader(file, self.limits), mode="r:"
                ) as archive:
                    _safe_extract_snapshot(archive, extract_dir, limits=self.limits)
            metadata = json.loads(
                self.limits.read_bytes(
                    extract_dir / SNAPSHOT_METADATA_FILENAME,
                    maximum=self.limits.max_metadata_bytes,
                    label="metadata",
                )
            )
            exported_state = load_exported_state(extract_dir / SNAPSHOT_STATE_FILENAME)
            exported_state_sha256 = _sha256_file(extract_dir / SNAPSHOT_STATE_FILENAME)
            _validate_archive_metadata(metadata, exported_state, self.chain_id)
            _validate_expected_checkpoint(
                exported_state, height=expected_height, app_hash_hex=expected_app_hash_hex
            )
            prepared = prepare_state_import(exported_state)
            state_root_hex = prepared.state_root.hex()
            if state_root_hex != str(exported_state["hash"]):
                raise ValueError("snapshot state root mismatch")
            imported_height = prepared.height
            imported_app_hash = state_root_hex
            archive_sha256 = _sha256_file(staged_archive)
            # Content identity avoids replacing a previous archive before its
            # replacement manifest and application state have been persisted.
            stored_snapshot_path = self.snapshot_dir / (
                f"xian-state-snapshot-h{imported_height}-{archive_sha256}.tar.gz"
            )
            staged_archive.replace(stored_snapshot_path)
            self._write_manifest(
                archive_path=stored_snapshot_path,
                height=imported_height,
                app_hash_hex=imported_app_hash,
                archive_sha256=archive_sha256,
                exported_state_sha256=exported_state_sha256,
            )

        # Artifact publication and staging cleanup finish before the durable
        # state replacement. A valid catalog entry is safe if the commit fails.
        result = import_state(
            exported_state=exported_state,
            storage_home=self.storage_home,
        )
        try:
            self._cleanup_existing_height(
                height=imported_height,
                format_version=SNAPSHOT_FORMAT_VERSION,
                keep_path=stored_snapshot_path,
            )
        except Exception as exc:
            logger.bind(**error_fields(exc)).warning(
                "Failed to clean up previous snapshots after state import"
            )
        return {
            "snapshot_path": str(snapshot_path),
            "stored_snapshot_path": str(stored_snapshot_path),
            "height": imported_height,
            "app_hash": imported_app_hash,
            **result,
        }
