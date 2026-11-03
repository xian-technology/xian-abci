"""A bounded, process-coordinated node log directory.

Uncompressed segments avoid transient duplicate disk usage during compression.
Only sink-owned names are removed. No descriptors to log segments remain open
between writes, so another process may rotate/prune safely under the same lock.
"""

from __future__ import annotations

import fcntl
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from xian.log_safety import safe_text

_OWNED = re.compile(r"xian-abci(?:-[0-9][0-9_.-]*)?\.log(?:\.zip)?\Z")


def _report_failure(operation: str, error: OSError) -> None:
    """Last-resort diagnostics must never recurse through a failing log sink."""
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    error_type = safe_text(type(error).__name__, 64)
    errno = error.errno
    errno = errno if type(errno) is int and errno.bit_length() <= 32 else "unknown"
    message = (
        f"{timestamp} | ERROR | xian-abci/log_retention | Log file operation failed"
        f" | operation={operation} error_type={error_type} errno={errno} pid={os.getpid()}\n"
    )
    try:
        sys.stderr.write(message)
        sys.stderr.flush()
    except Exception:
        # Stderr may itself be closed, broken, or unavailable. Retry normal file
        # work on the next call; a diagnostic failure must not stop the node.
        pass


class BoundedLogSink:
    def __init__(
        self,
        directory: Path,
        *,
        max_file_bytes: int,
        max_total_bytes: int,
        rotation_seconds: float,
        retention_seconds: float,
        cleanup_seconds: float = 3600,
    ):
        if (
            min(
                max_file_bytes,
                max_total_bytes,
                rotation_seconds,
                retention_seconds,
                cleanup_seconds,
            )
            <= 0
        ):
            raise ValueError("log limits must be positive")
        if max_total_bytes < max_file_bytes:
            raise ValueError("total log budget must cover one segment")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.active = self.directory / "xian-abci.log"
        self.max_file_bytes = max_file_bytes
        self.archive_budget = max_total_bytes - max_file_bytes
        self.rotation_seconds = rotation_seconds
        self.retention_seconds = retention_seconds
        self.cleanup_seconds = cleanup_seconds
        self._opened_at = time.monotonic()
        self._mutex = threading.Lock()
        self._last_write_warning: float | None = None
        self._stopped = threading.Event()
        with self._locked():
            self._rotate()
            self._cleanup()
        self._thread = threading.Thread(
            target=self._maintain, name="xian-log-retention", daemon=True
        )
        self._thread.start()

    @contextmanager
    def _locked(self):
        with self._mutex, (self.directory / ".xian-abci-log.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _owned_files(self):
        return [
            path
            for path in self.directory.iterdir()
            if _OWNED.fullmatch(path.name) and not path.is_symlink() and path.is_file()
        ]

    def _cleanup(self):
        cutoff = time.time() - self.retention_seconds
        archives = []
        for path in self._owned_files():
            stat = path.stat()
            if stat.st_mtime < cutoff:
                path.unlink()
            elif path != self.active:
                archives.append((stat.st_mtime_ns, path.name, stat.st_size, path))
        size = sum(item[2] for item in archives)
        for _, _, length, path in sorted(archives):
            if size <= self.archive_budget:
                break
            path.unlink()
            size -= length

    def _rotate(self):
        if self.active.exists() and not self.active.is_symlink():
            destination = self.directory / f"xian-abci-{time.time_ns()}-{os.getpid()}.log"
            self.active.rename(destination)
        self._opened_at = time.monotonic()

    def write(self, message):
        data = str(message).encode("utf-8")
        if len(data) > self.max_file_bytes:
            data = (
                data[: self.max_file_bytes - 1].decode("utf-8", errors="ignore").encode("utf-8")
                + b"\n"
            )
        try:
            self._write_data(data)
        except OSError as error:
            self._report_write_failure(error)

    def _report_write_failure(self, error: OSError) -> None:
        with self._mutex:
            now = time.monotonic()
            if self._last_write_warning is not None and now - self._last_write_warning < 60:
                return
            self._last_write_warning = now
        _report_failure("write", error)

    def _write_data(self, data: bytes) -> None:
        with self._locked():
            if self.active.is_symlink():
                raise OSError("refusing symlink as active application log")
            size = self.active.stat().st_size if self.active.exists() else 0
            if (
                size + len(data) > self.max_file_bytes
                or time.monotonic() - self._opened_at >= self.rotation_seconds
            ):
                self._rotate()
                self._cleanup()
            with self.active.open("ab") as output:
                output.write(data)

    def _maintain(self):
        while not self._stopped.wait(self.cleanup_seconds):
            try:
                with self._locked():
                    self._cleanup()
            except OSError as error:
                _report_failure("cleanup", error)

    def stop(self):
        self._stopped.set()
        self._thread.join(timeout=2)
