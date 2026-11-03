import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from xian.app_logging import configure_logging
from xian.node_setup import resolve_app_logging_settings


@pytest.mark.parametrize("key", ["rotation_hours", "retention_days", "max_file_mb", "max_total_mb"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "7"])
def test_logging_limits_require_positive_integers(key, value):
    with pytest.raises(ValueError):
        resolve_app_logging_settings(**{key: value})


def test_logging_budget_must_cover_segment():
    with pytest.raises(ValueError):
        resolve_app_logging_settings(max_file_mb=51, max_total_mb=50)


@pytest.mark.parametrize("structured", [False, True])
def test_output_is_utc_bounded_and_does_not_capture_secrets(tmp_path, structured):
    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {"app_log_json": structured})
    try:
        secret_local = "do-not-log-local-secret"
        try:
            raise ValueError("do-not-log-exception-secret")
        except ValueError:
            logger.bind(
                password="do-not-log-bound-secret",
                payload={"value": secret_local},
                stage="test\nforged",
            ).exception("Operation failed")
        logger.info("line\nforged\x1b[31m" + "x" * 20000)
        logger.complete()
    finally:
        logger.remove()
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.log"))
    assert "do-not-log-" not in text
    lines = text.splitlines()
    assert len(lines) == 3
    assert all(len(line.encode()) < 20000 for line in lines)
    assert "\x1b" not in text
    if structured:
        records = [json.loads(line) for line in lines]
        assert records[1]["record"]["extra"]["error_type"] == "ValueError"
        assert records[1]["record"]["time"]["repr"].endswith("+00:00")
    else:
        assert all(line[23:24] == "Z" for line in lines)
        assert "xian-abci/" in text
        assert "error_type=ValueError" in text


def test_rotation_budget_and_idle_age_cleanup(tmp_path):
    from xian.log_retention import BoundedLogSink

    unrelated = tmp_path / "operator-evidence.txt"
    unrelated.write_text("keep")
    sink = BoundedLogSink(
        tmp_path,
        max_file_bytes=100,
        max_total_bytes=250,
        rotation_seconds=3600,
        retention_seconds=1,
        cleanup_seconds=0.02,
    )
    try:
        for _ in range(20):
            sink.write("a" * 40 + "\n")
        files = list(tmp_path.glob("xian-abci*.log"))
        assert len(files) > 1
        assert all(p.stat().st_size <= 100 for p in files)
        assert sum(p.stat().st_size for p in files) <= 250
        for path in files:
            os.utime(path, (time.time() - 10,) * 2)
        deadline = time.monotonic() + 2
        while list(tmp_path.glob("xian-abci*.log")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not list(tmp_path.glob("xian-abci*.log"))
        sink.write("after cleanup\n")
        assert (tmp_path / "xian-abci.log").read_text() == "after cleanup\n"
        assert unrelated.read_text() == "keep"
    finally:
        sink.stop()


def _write_from_process(directory, count):
    from xian.log_retention import BoundedLogSink

    sink = BoundedLogSink(
        Path(directory),
        max_file_bytes=128,
        max_total_bytes=512,
        rotation_seconds=1,
        retention_seconds=60,
    )
    try:
        for index in range(count):
            sink.write(f"{os.getpid()} record={index}\n")
    finally:
        sink.stop()


def test_sink_coordinates_multiple_processes(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    children = [ctx.Process(target=_write_from_process, args=(str(tmp_path), 40)) for _ in range(3)]
    for child in children:
        child.start()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    files = list(tmp_path.glob("xian-abci*.log"))
    assert sum(path.stat().st_size for path in files) <= 512
    assert all(path.stat().st_size <= 128 for path in files)
    assert all(" record=" in line for path in files for line in path.read_text().splitlines())


def test_sink_rotates_by_time_and_preserves_unowned_names(tmp_path):
    from xian.log_retention import BoundedLogSink

    protected = tmp_path / "xian-abci-evidence.log"
    protected.write_text("evidence")
    sink = BoundedLogSink(
        tmp_path,
        max_file_bytes=1000,
        max_total_bytes=5000,
        rotation_seconds=0.01,
        retention_seconds=60,
    )
    try:
        sink.write("first\n")
        time.sleep(0.02)
        sink.write("second\n")
        assert (tmp_path / "xian-abci.log").read_text() == "second\n"
        assert any(p.read_text() == "first\n" for p in tmp_path.glob("xian-abci-*.log"))
        assert protected.read_text() == "evidence"
    finally:
        sink.stop()


def test_check_tx_failure_logs_reason_without_exception_payload(tmp_path):
    from xian.methods.check_tx import _rejected_check_tx

    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {})
    try:
        result = _rejected_check_tx(
            error=ValueError("secret-signed-request"), raw_tx=b"secret-signed-request"
        )
        _rejected_check_tx(error=ValueError("Bad signature"), raw_tx=b"bad")
        logger.complete()
    finally:
        logger.remove()
    text = (tmp_path / "logs" / "xian-abci.log").read_text()
    assert "secret-signed-request" not in text
    assert "reason_code=invalid_signature" in text
    assert "raw_tx_hash=" in text
    assert "error_type=ValueError" in text
    assert result.log == "ValueError: secret-signed-request"


def test_worker_logging_is_safe_without_creating_file_sink(tmp_path, monkeypatch):
    import io

    from xian.app_logging import configure_worker_logging

    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    configure_worker_logging()
    try:
        try:
            raise OSError(28, "secret-file-path")
        except OSError:
            logger.exception("Worker operation failed")
        logger.complete()
    finally:
        logger.remove()
    text = stream.getvalue()
    assert "secret-file-path" not in text
    assert "errno=28" in text
    assert "error_type=OSError" in text
    assert not list(tmp_path.iterdir())


def test_required_failure_fields_survive_context_limit(tmp_path):
    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {"app_log_json": True})
    try:
        fields = {f"field_{index}": "noise" for index in range(100)}
        fields.update(stage="write_block", tx_hash="abc123")
        try:
            raise OSError(28, "secret")
        except OSError:
            logger.bind(**fields).exception("Write failed")
        logger.complete()
    finally:
        logger.remove()
    text = (tmp_path / "logs" / "xian-abci.log").read_text()
    assert "stage=write_block" in text
    assert "tx_hash=abc123" in text
    assert "error_type=OSError" in text
    assert "errno=28" in text
    record = json.loads(text.splitlines()[-1])["record"]
    assert "test_required_failure_fields_survive_context_limit" in record["extra"]["stack"]
    assert "pid=" in text


def test_structured_nonfinite_context_is_valid_json(tmp_path):
    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {"app_log_json": True})
    try:
        logger.bind(duration=float("nan"), wait=float("inf")).info("Nonfinite telemetry")
        logger.complete()
    finally:
        logger.remove()

    def reject_constant(value):
        raise AssertionError(f"Non-JSON numeric constant: {value}")

    records = [
        json.loads(line, parse_constant=reject_constant)
        for line in (tmp_path / "logs" / "xian-abci.log").read_text().splitlines()
    ]
    assert records[-1]["record"]["extra"]["duration"] == "nan"


def test_rotation_occurs_only_at_exact_size_or_time_boundary(tmp_path, monkeypatch):
    from xian.log_retention import BoundedLogSink

    now = [100.0]
    monkeypatch.setattr("xian.log_retention.time.monotonic", lambda: now[0])
    sink = BoundedLogSink(
        tmp_path, max_file_bytes=100, max_total_bytes=500, rotation_seconds=10, retention_seconds=60
    )
    try:
        sink.write("a" * 50)
        now[0] = 109
        sink.write("b" * 50)
        assert (tmp_path / "xian-abci.log").read_text() == "a" * 50 + "b" * 50
        assert not list(tmp_path.glob("xian-abci-*.log"))
        now[0] = 110
        sink.write("")
        assert (tmp_path / "xian-abci.log").read_text() == ""
        assert len(list(tmp_path.glob("xian-abci-*.log"))) == 1
    finally:
        sink.stop()


def test_oversized_record_truncates_on_utf8_boundary(tmp_path):
    from xian.log_retention import BoundedLogSink

    sink = BoundedLogSink(
        tmp_path, max_file_bytes=10, max_total_bytes=10, rotation_seconds=60, retention_seconds=60
    )
    try:
        sink.write("猫" * 10)
        data = (tmp_path / "xian-abci.log").read_bytes()
        assert data.decode("utf-8") == "猫" * 3 + "\n"
        assert len(data) == 10
    finally:
        sink.stop()


def test_sink_preserves_symlink_target(tmp_path, capsys):
    from xian.log_retention import BoundedLogSink

    target = tmp_path / "operator-evidence"
    target.write_text("evidence")
    (tmp_path / "xian-abci.log").symlink_to(target)
    sink = BoundedLogSink(
        tmp_path, max_file_bytes=100, max_total_bytes=500, rotation_seconds=10, retention_seconds=60
    )
    try:
        sink.write("should not reach target")
        assert "error_type=OSError" in capsys.readouterr().err
        assert target.read_text() == "evidence"
    finally:
        sink.stop()


def test_context_redacts_compound_secrets_without_rendering_objects(tmp_path):
    class PrivateObject:
        def __repr__(self):
            raise AssertionError("must never render user object")

        __str__ = __repr__

    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {})
    try:
        logger.bind(
            access_token="credential-value",
            private_key="signing-value",
            signature=123456789,
            detail=PrivateObject(),
            empty=None,
            large=1 << 1000,
            flag=True,
            elapsed=1.5,
        ).info("Safe context")
        logger.complete()
    finally:
        logger.remove()
    text = (tmp_path / "logs" / "xian-abci.log").read_text()
    assert "credential-value" not in text
    assert "signing-value" not in text
    assert "123456789" not in text
    assert "detail=<PrivateObject>" in text
    assert 'large="<large integer>"' in text
    assert "flag=true" in text
    assert "elapsed=1.5" in text
    assert "empty=" not in text


def test_trace_logs_receipt_summary_without_payload(tmp_path):
    from contextlib import nullcontext

    from xian.methods.finalize_block import _assemble_tx_results

    app = SimpleNamespace(
        profiler=SimpleNamespace(scope=lambda *args, **kwargs: nullcontext()),
        nonce_storage=SimpleNamespace(set_nonce_by_tx=lambda tx: None),
        transaction_trace_debug_logging=True,
        transaction_trace_full_logging=True,
        bds_enabled=False,
    )
    tx = {
        "payload": {"sender": "alice", "contract": "currency", "function": "transfer", "nonce": 1}
    }
    receipt = {
        "hash": "ab" * 32,
        "status": 0,
        "chi_used": 5,
        "state": [{"key": "private", "value": "secret-state-value"}],
        "result": "secret-return-value",
        "events": [],
    }
    entry = {"block_tx_index": 0, "tx": tx, "tx_bytes": b"secret-envelope", "tx_result": receipt}
    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {"app_log_level": "TRACE"})
    try:
        results, _ = _assemble_tx_results(app, [entry], height=7, block_hash="cd" * 32)
        logger.complete()
    finally:
        logger.remove()
    text = (tmp_path / "logs" / "xian-abci.log").read_text()
    assert "secret-" not in text
    assert "stage=finalize_tx_result" in text
    assert "receipt_bytes=" in text
    assert "state_write_count=1" in text
    assert b"secret-state-value" in results[0].data
    assert b"secret-return-value" in results[0].data


def test_invalid_validator_logs_classification_without_state_payload(tmp_path, caplog):
    from xian.validators import ValidatorHandler

    state = {"validators.active_validators": ["secret-invalid-validator\nforged"]}
    driver = SimpleNamespace(
        get=state.get,
        value_from_disk=state.get,
        set=lambda key, value: state.__setitem__(key, value),
    )
    handler = ValidatorHandler(SimpleNamespace(client=SimpleNamespace(raw_driver=driver)))
    configure_logging(SimpleNamespace(STORAGE_HOME=tmp_path), {})
    try:
        assert handler.build_validator_updates(height=42) == []
        logger.complete()
    finally:
        logger.remove()
    text = (tmp_path / "logs" / "xian-abci.log").read_text() + caplog.text
    assert "secret-invalid-validator" not in text
    assert "reason_code=invalid_validator_key" in text
    assert "reason_code=empty_validator_selection" in text
    assert "block_height=42" in text


def test_abci_startup_error_uses_safe_context_even_without_patcher(tmp_path, monkeypatch):
    import asyncio
    import io

    from abci.server import ABCIServer

    async def fail_start(self):
        raise OSError(98, "secret-startup-credential")

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(ABCIServer, "_start", fail_start)
    stream = io.StringIO()
    logger.remove()
    logger.configure(patcher=None)
    logger.add(stream, format="{message} {extra}", diagnose=False, backtrace=False)
    try:
        ABCIServer(SimpleNamespace()).run()
    finally:
        logger.remove()
    text = stream.getvalue()
    assert "secret-startup-credential" not in text
    assert "OSError" in text
    assert "98" in text
    assert "ABCI server startup failed" in text
