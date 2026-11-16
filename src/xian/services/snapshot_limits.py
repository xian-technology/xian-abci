"""Local resource policy for snapshot transfers; never part of chain consensus."""

from __future__ import annotations

import gzip
import shutil
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import BinaryIO

DEFAULT_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_METADATA_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
TAR_OVERHEAD_BYTES = 64 * 1024


@dataclass(frozen=True)
class SnapshotLimits:
    max_state_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES
    max_archive_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES
    min_free_disk_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            minimum = 0 if field.name == "min_free_disk_bytes" else 1
            if type(value) is not int or not minimum <= value <= sys.maxsize:
                raise ValueError(f"snapshot_{field.name} must be an integer >= {minimum}")

    @classmethod
    def from_config(cls, config: dict) -> SnapshotLimits:
        return cls(
            **{
                field.name: config["snapshot_" + field.name]
                for field in fields(cls)
                if "snapshot_" + field.name in config
            }
        )

    @property
    def max_unpacked_bytes(self) -> int:
        return self.max_state_bytes + self.max_metadata_bytes + TAR_OVERHEAD_BYTES

    def check_size(self, size: int, maximum: int, label: str) -> None:
        if size < 0 or size > maximum:
            raise ValueError(f"snapshot {label} exceeds configured limit ({maximum} bytes)")

    def require_disk_space(self, directory: Path, additional_bytes: int) -> None:
        if shutil.disk_usage(directory).free < additional_bytes + self.min_free_disk_bytes:
            raise ValueError("insufficient free disk space for snapshot staging")

    def validate_member_size(self, name: str, size: int) -> None:
        maximum = self.max_metadata_bytes if name == "metadata.json" else self.max_state_bytes
        self.check_size(size, maximum, name)

    def copy_bounded(self, source: BinaryIO, destination: Path, *, maximum: int, label: str) -> int:
        total = 0
        with destination.open("xb") as target:
            while data := source.read(min(COPY_CHUNK_BYTES, maximum - total + 1)):
                total += len(data)
                self.check_size(total, maximum, label)
                self.require_disk_space(destination.parent, len(data))
                target.write(data)
        return total

    def stage_archive(self, source: Path, destination: Path) -> None:
        size = source.stat().st_size
        self.check_size(size, self.max_archive_bytes, "archive")
        self.require_disk_space(destination.parent, size)
        with source.open("rb") as file:
            self.copy_bounded(file, destination, maximum=self.max_archive_bytes, label="archive")

    def unpack_archive(self, source: Path, destination: Path) -> None:
        with gzip.open(source, "rb") as file:
            self.copy_bounded(
                file, destination, maximum=self.max_unpacked_bytes, label="uncompressed archive"
            )

    def read_bytes(self, path: Path, *, maximum: int, label: str) -> bytes:
        size = path.stat().st_size
        self.check_size(size, maximum, label)
        with path.open("rb") as file:
            data = file.read(size + 1)
        self.check_size(len(data), maximum, label)
        if len(data) != size:
            raise ValueError(f"snapshot {label} changed while reading")
        return data


class SnapshotArchiveReader:
    """Bound auxiliary tar headers before tarfile parses their declared payloads."""

    def __init__(self, file: BinaryIO, limits: SnapshotLimits):
        self.file = file
        self.maximum_read = max(limits.max_metadata_bytes, 16 * 1024)
        self.header_bytes_remaining: int | None = 2 * self.maximum_read + TAR_OVERHEAD_BYTES

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self.maximum_read:
            raise ValueError("snapshot tar header read exceeds configured limit")
        if self.header_bytes_remaining is not None:
            if size > self.header_bytes_remaining:
                raise ValueError("snapshot tar headers exceed configured limit")
            self.header_bytes_remaining -= size
        return self.file.read(size)

    def allow_payload_reads(self) -> None:
        self.header_bytes_remaining = None

    def __getattr__(self, name):
        return getattr(self.file, name)
