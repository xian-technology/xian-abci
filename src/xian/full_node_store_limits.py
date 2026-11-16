"""Operator-adjustable full-store transfer limits, independent of consensus."""

from __future__ import annotations

import gzip
import hashlib
import shutil
from dataclasses import dataclass, fields
from pathlib import Path
from typing import BinaryIO

CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


@dataclass(frozen=True)
class FullNodeStoreLimits:
    max_archive_bytes: int = 8 * 1024**3
    max_uncompressed_bytes: int = 32 * 1024**3
    min_free_disk_bytes: int = 256 * 1024**2
    max_members: int = 100_000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            minimum = 0 if field.name == "min_free_disk_bytes" else 1
            if type(value) is not int or not minimum <= value <= (1 << 63) - 1:
                raise ValueError(f"full-store {field.name} must be an integer >= {minimum}")

    def require_disk_space(self, directory: Path, additional_bytes: int):
        if shutil.disk_usage(directory).free < additional_bytes + self.min_free_disk_bytes:
            raise ValueError("insufficient free disk space for full-node-store staging")

    def check_size(self, size: int, maximum: int, label: str):
        if size < 0 or size > maximum:
            raise ValueError(f"full-node-store {label} exceeds configured limit ({maximum} bytes)")

    def copy_stream(
        self,
        source: BinaryIO,
        destination: Path,
        *,
        maximum: int,
        expected_bytes: int | None = None,
        label: str,
    ) -> tuple[int, str]:
        total, digest = 0, hashlib.sha256()
        with destination.open("xb") as target:
            while data := source.read(min(CHUNK_BYTES, maximum - total + 1)):
                total += len(data)
                self.check_size(total, maximum, label)
                self.require_disk_space(destination.parent, len(data))
                digest.update(data)
                target.write(data)
        if expected_bytes is not None and total != expected_bytes:
            raise ValueError(f"full-node-store {label} byte count mismatch")
        return total, digest.hexdigest()

    def unpack_archive(self, source: Path, destination: Path, *, expected_bytes: int | None):
        maximum = self.max_uncompressed_bytes
        if expected_bytes is not None:
            self.check_size(expected_bytes, maximum, "uncompressed archive")
            maximum = expected_bytes
            # One tar copy plus extracted data coexist until publication.
            self.require_disk_space(destination.parent, 2 * expected_bytes)
        with source.open("rb") as file:
            magic = file.read(2)
            file.seek(0)
            if magic == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=file) as unpacked:
                    return self.copy_stream(
                        unpacked,
                        destination,
                        maximum=maximum,
                        expected_bytes=expected_bytes,
                        label="uncompressed archive",
                    )
            return self.copy_stream(
                file,
                destination,
                maximum=maximum,
                expected_bytes=expected_bytes,
                label="uncompressed archive",
            )
