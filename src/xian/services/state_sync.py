from __future__ import annotations

import hashlib
import json
import math
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from loguru import logger

from cometbft.abci.v1beta1.types_pb2 import (
    ResponseApplySnapshotChunk,
    ResponseListSnapshots,
    ResponseLoadSnapshotChunk,
    ResponseOfferSnapshot,
    Snapshot,
)
from xian.state_export import (
    export_state,
    import_state,
    load_exported_state,
)
from xian.state_root import compute_exported_state_root

SNAPSHOT_FORMAT_VERSION = 1
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
SNAPSHOT_METADATA_FILENAME = "metadata.json"
SNAPSHOT_STATE_FILENAME = "exported_state.json"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def default_snapshot_output_path(
    *,
    output_dir: Path,
    height: int,
    app_hash: str,
    now: datetime | None = None,
) -> Path:
    resolved_now = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    short_hash = app_hash[:12] if app_hash else "empty"
    return output_dir / (
        f"xian-state-snapshot-h{height}-{short_hash}-{resolved_now}.tar.gz"
    )


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

    def to_proto(self) -> Snapshot:
        return Snapshot(
            height=self.height,
            format=self.format,
            chunks=self.chunks,
            hash=bytes.fromhex(self.snapshot_hash_hex),
            metadata=self.metadata_bytes,
        )


@dataclass
class IncomingSnapshotSession:
    temp_dir: TemporaryDirectory[str]
    expected_height: int
    expected_format: int
    expected_chunks: int
    expected_chunk_size: int
    expected_app_hash_hex: str
    expected_snapshot_hash_hex: str
    next_index: int = 0

    @property
    def temp_root(self) -> Path:
        return Path(self.temp_dir.name)

    @property
    def archive_path(self) -> Path:
        return self.temp_root / "incoming-snapshot.tar.gz"

    @property
    def extract_dir(self) -> Path:
        return self.temp_root / "extracted"

    def close(self) -> None:
        self.temp_dir.cleanup()


class StateSnapshotManager:
    def __init__(
        self,
        *,
        storage_home: Path,
        chain_id: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.storage_home = Path(storage_home)
        self.chain_id = chain_id
        self.chunk_size = chunk_size
        self.snapshot_dir = self.storage_home / "snapshots"
        self.incoming_dir = self.snapshot_dir / "incoming"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self._incoming_session: IncomingSnapshotSession | None = None

    def _manifest_path(self, archive_path: Path) -> Path:
        return archive_path.parent / f"{archive_path.name}.manifest.json"

    def _cleanup_existing_height(
        self, *, height: int, format_version: int
    ) -> None:
        for record in self.list_snapshot_records():
            if record.height != height or record.format != format_version:
                continue
            logger.info(
                "Removing previous snapshot for height={} format={}: {}",
                height,
                format_version,
                record.path,
            )
            record.path.unlink(missing_ok=True)
            record.manifest_path.unlink(missing_ok=True)

    def _build_offer_metadata(
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
        metadata_bytes = self._build_offer_metadata(
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
        manifest_path.write_text(
            json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
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
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        records.sort(
            key=lambda record: (record.height, record.created_at), reverse=True
        )
        return records

    def list_snapshots_response(self) -> ResponseListSnapshots:
        return ResponseListSnapshots(
            snapshots=[
                record.to_proto() for record in self.list_snapshot_records()
            ]
        )

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
            exported_state = load_exported_state(exported_state_path)
            height = int(exported_state["number"])
            app_hash_hex = str(exported_state["hash"])
            exported_state_sha256 = _sha256_file(exported_state_path)
            self._cleanup_existing_height(
                height=height,
                format_version=SNAPSHOT_FORMAT_VERSION,
            )
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
                raise FileExistsError(
                    f"snapshot already exists: {archive_path}"
                )

            metadata_path = temp_root / SNAPSHOT_METADATA_FILENAME
            metadata_path.write_bytes(
                _json_bytes(
                    {
                        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
                        "chain_id": self.chain_id,
                        "height": height,
                        "app_hash": app_hash_hex,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
            )

            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(
                    metadata_path,
                    arcname=SNAPSHOT_METADATA_FILENAME,
                )
                archive.add(
                    exported_state_path,
                    arcname=SNAPSHOT_STATE_FILENAME,
                )

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
        return {
            "output_path": str(record.path),
            "height": record.height,
            "app_hash": record.app_hash_hex,
            "format": record.format,
            "chunks": record.chunks,
        }

    def import_snapshot_archive(self, snapshot_path: Path) -> dict[str, Any]:
        if not snapshot_path.exists():
            raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
        exported_state_sha256: str | None = None
        imported_height: int | None = None
        imported_app_hash: str | None = None
        with TemporaryDirectory(dir=self.incoming_dir) as temp_dir:
            temp_root = Path(temp_dir)
            extract_dir = temp_root / "extracted"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(snapshot_path, "r:gz") as archive:
                _safe_extract_snapshot(archive, extract_dir)
            metadata = json.loads(
                (extract_dir / SNAPSHOT_METADATA_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            exported_state = load_exported_state(
                extract_dir / SNAPSHOT_STATE_FILENAME
            )
            exported_state_sha256 = _sha256_file(
                extract_dir / SNAPSHOT_STATE_FILENAME
            )
            if (
                int(metadata["snapshot_format_version"])
                != SNAPSHOT_FORMAT_VERSION
            ):
                raise ValueError("unsupported snapshot format version")
            if str(metadata["chain_id"]) != self.chain_id:
                raise ValueError("snapshot chain_id mismatch")
            if int(metadata["height"]) != int(exported_state["number"]):
                raise ValueError("snapshot height metadata mismatch")
            if str(metadata["app_hash"]) != str(exported_state["hash"]):
                raise ValueError("snapshot app hash metadata mismatch")
            state_root_hex = compute_exported_state_root(exported_state).hex()
            if state_root_hex != str(exported_state["hash"]):
                raise ValueError("snapshot state root mismatch")
            result = import_state(
                exported_state=exported_state,
                storage_home=self.storage_home,
            )
            imported_height = int(exported_state["number"])
            imported_app_hash = str(exported_state["hash"])

        assert exported_state_sha256 is not None
        assert imported_height is not None
        assert imported_app_hash is not None
        self._cleanup_existing_height(
            height=imported_height,
            format_version=SNAPSHOT_FORMAT_VERSION,
        )
        stored_snapshot_path = default_snapshot_output_path(
            output_dir=self.snapshot_dir,
            height=imported_height,
            app_hash=imported_app_hash,
        )
        stored_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, stored_snapshot_path)
        archive_sha256 = _sha256_file(stored_snapshot_path)
        self._write_manifest(
            archive_path=stored_snapshot_path,
            height=imported_height,
            app_hash_hex=imported_app_hash,
            archive_sha256=archive_sha256,
            exported_state_sha256=exported_state_sha256,
        )
        return {
            "snapshot_path": str(snapshot_path),
            "stored_snapshot_path": str(stored_snapshot_path),
            "height": imported_height,
            "app_hash": imported_app_hash,
            **result,
        }

    def offer_snapshot_response(
        self,
        req_snapshot: Snapshot,
        *,
        app_hash: bytes,
        current_height: int,
    ) -> ResponseOfferSnapshot:
        if req_snapshot.format != SNAPSHOT_FORMAT_VERSION:
            return ResponseOfferSnapshot(
                result=ResponseOfferSnapshot.REJECT_FORMAT
            )
        if req_snapshot.height <= current_height:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if req_snapshot.chunks <= 0:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if len(req_snapshot.hash) != 32:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        try:
            metadata = json.loads(req_snapshot.metadata.decode("utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("snapshot metadata must be an object")
            metadata_height = int(metadata.get("height", -1))
            metadata_chunk_size = int(metadata.get("chunk_size", 0))
        except Exception:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)

        if metadata.get("snapshot_format_version") != SNAPSHOT_FORMAT_VERSION:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if metadata.get("chain_id") != self.chain_id:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if metadata_height != req_snapshot.height:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if metadata_chunk_size <= 0 or metadata_chunk_size > self.chunk_size:
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if metadata.get("archive_sha256") != req_snapshot.hash.hex():
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)
        if metadata.get("app_hash") != app_hash.hex():
            return ResponseOfferSnapshot(result=ResponseOfferSnapshot.REJECT)

        self._reset_incoming_session()
        self._incoming_session = IncomingSnapshotSession(
            temp_dir=TemporaryDirectory(dir=self.incoming_dir),
            expected_height=req_snapshot.height,
            expected_format=req_snapshot.format,
            expected_chunks=req_snapshot.chunks,
            expected_chunk_size=metadata_chunk_size,
            expected_app_hash_hex=app_hash.hex(),
            expected_snapshot_hash_hex=req_snapshot.hash.hex(),
        )
        return ResponseOfferSnapshot(result=ResponseOfferSnapshot.ACCEPT)

    def load_snapshot_chunk_response(
        self,
        *,
        height: int,
        format_version: int,
        chunk_index: int,
    ) -> ResponseLoadSnapshotChunk:
        record = next(
            (
                candidate
                for candidate in self.list_snapshot_records()
                if candidate.height == height
                and candidate.format == format_version
            ),
            None,
        )
        if record is None or chunk_index < 0 or chunk_index >= record.chunks:
            return ResponseLoadSnapshotChunk(chunk=b"")

        with open(record.path, "rb") as handle:
            handle.seek(chunk_index * record.chunk_size)
            chunk = handle.read(record.chunk_size)
        return ResponseLoadSnapshotChunk(chunk=chunk)

    def apply_snapshot_chunk_response(
        self,
        *,
        index: int,
        chunk: bytes,
        sender: str,
    ) -> ResponseApplySnapshotChunk:
        del sender
        if self._incoming_session is None:
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.RETRY_SNAPSHOT
            )
        session = self._incoming_session
        if index != session.next_index:
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.RETRY
            )
        if index < 0 or index >= session.expected_chunks:
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.REJECT_SNAPSHOT
            )
        if len(chunk) > session.expected_chunk_size:
            self._reset_incoming_session()
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.REJECT_SNAPSHOT
            )

        with open(session.archive_path, "ab") as handle:
            handle.write(chunk)
        session.next_index += 1

        if session.next_index < session.expected_chunks:
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.ACCEPT
            )

        try:
            archive_sha256 = _sha256_file(session.archive_path)
            if archive_sha256 != session.expected_snapshot_hash_hex:
                raise ValueError("snapshot archive hash mismatch")
            result = self.import_snapshot_archive(session.archive_path)
            if result["app_hash"] != session.expected_app_hash_hex:
                raise ValueError("snapshot app hash mismatch")
        except Exception as exc:
            logger.error("Failed to apply state sync snapshot: {}", exc)
            self._reset_incoming_session()
            return ResponseApplySnapshotChunk(
                result=ResponseApplySnapshotChunk.REJECT_SNAPSHOT
            )

        self._reset_incoming_session()
        return ResponseApplySnapshotChunk(
            result=ResponseApplySnapshotChunk.ACCEPT
        )

    def _reset_incoming_session(self) -> None:
        if self._incoming_session is not None:
            self._incoming_session.close()
            self._incoming_session = None
