"""File logging failures stay observable without leaking values or stopping work."""

import errno
import io
import threading
from pathlib import Path

import pytest
from loguru import logger

from xian.log_retention import BoundedLogSink


def make_sink(directory, *, cleanup_seconds=3600):
    return BoundedLogSink(
        directory,
        max_file_bytes=1024,
        max_total_bytes=4096,
        rotation_seconds=3600,
        retention_seconds=1,
        cleanup_seconds=cleanup_seconds,
    )


def test_idle_cleanup_failure_is_reported_safely_then_recovers(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    sink = make_sink(tmp_path, cleanup_seconds=0.02)
    failed = threading.Event()
    recovered = threading.Event()
    original_cleanup = sink._cleanup

    def intermittent_cleanup():
        if not failed.is_set():
            failed.set()
            raise PermissionError(errno.EACCES, "private-retention-path")
        original_cleanup()
        recovered.set()

    monkeypatch.setattr(sink, "_cleanup", intermittent_cleanup)
    try:
        assert recovered.wait(2), "maintenance must retry after cleanup failure"
        text = stream.getvalue()
        assert text.count("Log file operation failed") == 1
        assert "private-retention-path" not in text
        assert "operation=cleanup" in text
        assert "error_type=PermissionError" in text
        assert "errno=13" in text
        assert text[23:24] == "Z"
        assert len(text.encode()) < 512
        assert len(text.splitlines()) == 1
    finally:
        sink.stop()


@pytest.mark.parametrize("broken_at", ["write", "flush"])
def test_broken_fallback_stderr_does_not_stop_cleanup(tmp_path, monkeypatch, broken_at):
    fallback_attempts = []

    class BrokenStream:
        def write(self, _text):
            fallback_attempts.append(True)
            if broken_at == "write":
                raise BrokenPipeError("private-stderr-message")

        def flush(self):
            if broken_at == "flush":
                raise ValueError("closed stream")

    monkeypatch.setattr("sys.stderr", BrokenStream())
    sink = make_sink(tmp_path, cleanup_seconds=0.02)
    attempts = []
    completed = threading.Event()

    def retry_cleanup():
        attempts.append(True)
        if len(attempts) < 3:
            raise OSError(errno.ENOSPC, "private-disk-message")
        completed.set()

    monkeypatch.setattr(sink, "_cleanup", retry_cleanup)
    try:
        assert completed.wait(2), "broken diagnostics must not terminate maintenance"
        assert sink._thread.is_alive()
        assert len(fallback_attempts) == 2
    finally:
        sink.stop()


def test_write_failure_avoids_loguru_dump_and_throttles_then_recovers(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    now = [100.0]
    monkeypatch.setattr("xian.log_retention.time.monotonic", lambda: now[0])
    sink = make_sink(tmp_path)
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path == sink.active:
            raise OSError(errno.ENOSPC, "private-write-path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    logger.remove()
    logger.configure(patcher=None)
    handler = logger.add(sink, format="{message}", catch=True)
    try:
        for _ in range(10):
            logger.info("private-original-record")
        assert stream.getvalue().count("Log file operation failed") == 1
        now[0] = 160.0
        logger.info("private-original-record")
        text = stream.getvalue()
        assert text.count("Log file operation failed") == 2
        assert "private-" not in text
        assert "operation=write" in text
        assert "errno=28" in text
        monkeypatch.setattr(Path, "open", original_open)
        logger.info("record after recovery")
        assert sink.active.read_text() == "record after recovery\n"
    finally:
        logger.remove(handler)
