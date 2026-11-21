"""Snapshot peers and archives cannot request unbounded local resources."""

import gzip
import io
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from xian.services.snapshot_limits import SnapshotArchiveReader, SnapshotLimits
from xian.services.state_snapshot import (
    StateSnapshotManager,
    _safe_extract_snapshot,
)


def test_oversized_compressed_archive_is_refused_before_copy(tmp_path):
    archive = tmp_path / "oversized.tar.gz"
    with archive.open("wb") as file:
        file.seek(256 * 1024 * 1024)
        file.write(b"x")
    manager = StateSnapshotManager(storage_home=tmp_path / "node", chain_id="local")
    with patch("xian.services.state_snapshot.SnapshotLimits.stage_archive") as copy:
        with pytest.raises(ValueError, match="archive.*limit"):
            manager.import_snapshot_archive(archive)
    copy.assert_not_called()
    assert list(manager.snapshot_dir.iterdir()) == []


@pytest.mark.parametrize("oversized", ["metadata.json", "exported_state.json"])
def test_member_size_is_checked_before_extraction(tmp_path, oversized):
    members = []
    for name in ("metadata.json", "exported_state.json"):
        member = tarfile.TarInfo(name)
        member.size = 256 * 1024 * 1024 + 1 if name == oversized else 2
        members.append(member)
    archive = SimpleNamespace(__iter__=None, extractall=Mock())

    class ReadableArchive:
        extractall = archive.extractall

        def __iter__(self):
            return iter(members)

    with pytest.raises(ValueError, match="snapshot.*limit"):
        _safe_extract_snapshot(ReadableArchive(), tmp_path)
    archive.extractall.assert_not_called()


def test_compressed_bomb_is_bounded_before_tar_parsing_and_staging_is_cleaned(tmp_path):
    limits = SnapshotLimits(max_state_bytes=128, max_metadata_bytes=128, min_free_disk_bytes=0)
    archive = tmp_path / "bomb.tar.gz"
    archive.write_bytes(gzip.compress(b"\0" * (limits.max_unpacked_bytes + 1)))
    manager = StateSnapshotManager(storage_home=tmp_path / "node", chain_id="local", limits=limits)

    with patch("xian.services.state_snapshot.tarfile.open") as parse:
        with pytest.raises(ValueError, match="uncompressed archive.*limit"):
            manager.import_snapshot_archive(archive)

    parse.assert_not_called()
    assert list(manager.snapshot_dir.iterdir()) == []


def test_large_auxiliary_tar_header_is_refused_before_its_payload_is_buffered(tmp_path):
    header = tarfile.TarInfo("././@PaxHeader")
    header.type = tarfile.XHDTYPE
    header.size = 1024 * 1024
    archive = tmp_path / "pax.tar.gz"
    archive.write_bytes(gzip.compress(header.tobuf() + b"\0" * 1024))
    manager = StateSnapshotManager(storage_home=tmp_path / "node", chain_id="local")

    with pytest.raises(ValueError, match="tar header read.*limit"):
        manager.import_snapshot_archive(archive)

    assert list(manager.snapshot_dir.iterdir()) == []


def test_low_disk_import_cleans_up_temporary_staging(tmp_path):
    archive = tmp_path / "local.tar.gz"
    archive.write_bytes(b"archive")
    manager = StateSnapshotManager(storage_home=tmp_path / "node", chain_id="local")
    with (
        patch(
            "xian.services.snapshot_limits.shutil.disk_usage", return_value=SimpleNamespace(free=0)
        ),
        pytest.raises(ValueError, match="insufficient free disk space"),
    ):
        manager.import_snapshot_archive(archive)
    assert list(manager.snapshot_dir.iterdir()) == []


@pytest.mark.parametrize("value", [0, -1, True, "1024", 1.5])
def test_invalid_operator_limits_fail_before_creating_snapshot_directories(tmp_path, value):
    with pytest.raises(ValueError, match="snapshot_max_state_bytes"):
        StateSnapshotManager.from_config(
            storage_home=tmp_path,
            chain_id="local",
            config={"snapshot_max_state_bytes": value},
        )
    assert not (tmp_path / "snapshots").exists()


def test_raised_operator_cap_imports_a_valid_snapshot_without_changing_chain_rules(tmp_path):
    from contracting.storage.driver import Driver

    from xian.evidence_policy import EVIDENCE_POLICY_KEY, EvidencePolicy
    from xian.execution_policy import EXECUTION_POLICY_KEY, current_execution_policy
    from xian.state_root import compute_driver_state_root
    from xian.utils.block import stage_latest_block

    source_home = tmp_path / "source"
    with Driver(vm_profile="xian_vm_v2", storage_home=source_home) as driver:
        driver.set(EXECUTION_POLICY_KEY, current_execution_policy())
        driver.set(EVIDENCE_POLICY_KEY, EvidencePolicy.seed(7, nanos=0).to_state())
        driver.set("con_snapshot.value", "x" * 16_384)
        stage_latest_block(driver, block_hash=compute_driver_state_root(driver), height=42, nanos=0)
        driver.commit()
    source = StateSnapshotManager(storage_home=source_home, chain_id="local")
    archive = source.export_snapshot()["output_path"]
    target_home = tmp_path / "target"
    limited = StateSnapshotManager(
        storage_home=target_home, chain_id="local", limits=SnapshotLimits(max_state_bytes=1024)
    )
    with pytest.raises(ValueError, match="exported_state.json.*limit"):
        limited.import_snapshot_archive(Path(archive))
    assert list(limited.snapshot_dir.iterdir()) == []

    raised = StateSnapshotManager.from_config(
        storage_home=target_home,
        chain_id="local",
        config={"snapshot_max_state_bytes": 128 * 1024, "snapshot_max_archive_bytes": 128 * 1024},
    )
    result = raised.import_snapshot_archive(Path(archive))

    assert result["height"] == 42
    with Driver(vm_profile="xian_vm_v2", storage_home=target_home) as driver:
        assert driver.get("con_snapshot.value") == "x" * 16_384


def test_snapshot_cli_passes_explicit_resource_overrides(monkeypatch, capsys):
    from xian.cli import state_snapshot

    manager = SimpleNamespace(list_snapshot_records=lambda: [])
    factory = Mock(return_value=manager)
    monkeypatch.setattr(state_snapshot, "_build_manager", factory)

    assert (
        state_snapshot.main(["list", "--max-state-bytes", "512", "--max-archive-bytes", "1024"])
        == 0
    )

    factory.assert_called_once_with(
        {"snapshot_max_state_bytes": 512, "snapshot_max_archive_bytes": 1024}
    )
    assert capsys.readouterr().out.strip() == "[]"


@pytest.mark.parametrize("size", [-1, 6])
def test_byte_limits_are_nonnegative_and_inclusive(size):
    limits = SnapshotLimits()
    limits.check_size(5, 5, "test")
    with pytest.raises(ValueError):
        limits.check_size(size, 5, "test")


@pytest.mark.parametrize("free, accepted", [(14, False), (15, True)])
def test_disk_reservation_is_inclusive(tmp_path, free, accepted):
    limits = SnapshotLimits(min_free_disk_bytes=10)
    with patch(
        "xian.services.snapshot_limits.shutil.disk_usage", return_value=SimpleNamespace(free=free)
    ):
        if accepted:
            limits.require_disk_space(tmp_path, 5)
        else:
            with pytest.raises(ValueError, match="free disk"):
                limits.require_disk_space(tmp_path, 5)


def test_auxiliary_header_total_is_bounded_until_extraction_begins():
    reader = SnapshotArchiveReader(
        io.BytesIO(b"x" * 131_072), SnapshotLimits(max_metadata_bytes=1024)
    )
    for _ in range(6):
        assert len(reader.read(16_384)) == 16_384
    with pytest.raises(ValueError, match="tar headers"):
        reader.read(1)
    reader.allow_payload_reads()
    assert len(reader.read(16_384)) == 16_384
    with pytest.raises(ValueError, match="tar header read"):
        reader.read()


def test_copy_stops_before_writing_bytes_above_the_cap(tmp_path):
    limits = SnapshotLimits(min_free_disk_bytes=0)
    output = tmp_path / "bounded"
    with pytest.raises(ValueError, match="limit"):
        limits.copy_bounded(io.BytesIO(b"123456"), output, maximum=5, label="test")
    assert output.stat().st_size <= 5


def test_raised_metadata_limit_does_not_allocate_the_entire_limit(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(b"{}")
    limits = SnapshotLimits(max_metadata_bytes=sys.maxsize)

    assert limits.read_bytes(metadata, maximum=limits.max_metadata_bytes, label="metadata") == b"{}"

    with pytest.raises(ValueError):
        SnapshotLimits(max_metadata_bytes=sys.maxsize + 1)


def test_metadata_growth_after_stat_is_not_silently_truncated(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(b"{}trailing")
    with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_size=2)):
        with pytest.raises(ValueError, match="changed while reading"):
            SnapshotLimits().read_bytes(metadata, maximum=64, label="metadata")


@pytest.mark.parametrize("payload", [b"", b"12345"])
def test_copy_accepts_an_exact_limit_and_never_overreads_the_remaining_budget(tmp_path, payload):
    class BoundedSource(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 6 - self.tell()
            return super().read(size)

    limits = SnapshotLimits(min_free_disk_bytes=0)
    output = tmp_path / "exact"
    assert limits.copy_bounded(BoundedSource(payload), output, maximum=5, label="test") == len(
        payload
    )
    assert output.read_bytes() == payload
    limits.check_size(0, 5, "test")


def test_zero_length_tar_read_is_safe_and_does_not_consume_header_budget():
    reader = SnapshotArchiveReader(io.BytesIO(b"payload"), SnapshotLimits())
    before = reader.header_bytes_remaining
    assert reader.read(0) == b""
    assert reader.header_bytes_remaining == before
