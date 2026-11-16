"""Archive-format boundary scenarios using actual staged application LMDB."""

import gzip
import hashlib
import io
import tarfile
from unittest.mock import patch

import pytest
from full_node_store_fixtures import Response, fixture_inspector
from test_full_node_store import stores as stores

from xian.node_admin import apply_snapshot_archive


def test_uncompressed_tar_receives_the_same_checkpoint_checks(stores):
    _, target, _, compressed, *_ = stores
    payload = gzip.decompress(compressed)
    with patch("xian.node_admin.urlopen", return_value=Response(payload)):
        apply_snapshot_archive(
            "https://invalid/store.tar",
            target,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            native_store_inspector=fixture_inspector,
        )
    assert (target / "data/native-checkpoint.json").is_file()


@pytest.mark.parametrize("header_type", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_auxiliary_tar_header_is_bounded_before_parsing(stores, header_type):
    _, target, _, original, *_ = stores
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("pax")
        member.type = header_type
        member.size = 131072
        prefix = b"131072 comment="
        content = (
            prefix + b"x" * (member.size - len(prefix) - 1) + b"\n"
            if header_type == tarfile.XHDTYPE
            else b"x" * (member.size - 1) + b"\0"
        )
        archive.addfile(member, io.BytesIO(content))
        with tarfile.open(fileobj=io.BytesIO(original)) as source:
            for existing in source:
                archive.addfile(
                    existing, source.extractfile(existing) if existing.isfile() else None
                )
    payload = output.getvalue()
    with patch("xian.node_admin.urlopen", return_value=Response(payload)):
        with pytest.raises(
            (ValueError, tarfile.ReadError), match="tar header read exceeds|invalid header"
        ):
            apply_snapshot_archive(
                "https://invalid/s.tar.gz",
                target,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                native_store_inspector=fixture_inspector,
            )
    assert (target / "data/original").exists()


def test_direct_store_reader_rejects_interrupted_source(stores):
    from xian.full_node_store import read_full_node_store_checkpoint

    source, *_ = stores
    marker = source / ".snapshot-restore-old/publication-started"
    marker.parent.mkdir()
    marker.touch()
    with pytest.raises(ValueError, match="incomplete snapshot restore"):
        read_full_node_store_checkpoint(source, native_store_inspector=fixture_inspector)


@pytest.mark.parametrize("size", [65536, 65537, 262163])
@pytest.mark.parametrize("compressed", [False, True])
def test_large_regular_files_restore_exactly_without_weakening_header_limits(
    stores, size, compressed
):
    _, target, _, original, *_ = stores
    content = (bytes(range(251)) * (size // 251 + 1))[:size]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz" if compressed else "w:") as archive:
        for name, value in (
            ("data/large.db", content),
            ("data/empty.db", b""),
            ("data/after.db", b"after large member"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        with tarfile.open(fileobj=io.BytesIO(original)) as source:
            for member in source:
                archive.addfile(member, source.extractfile(member) if member.isfile() else None)
    payload = output.getvalue()
    with patch("xian.node_admin.urlopen", return_value=Response(payload)):
        apply_snapshot_archive(
            "https://invalid/store.tar",
            target,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            native_store_inspector=fixture_inspector,
        )
    assert (target / "data/large.db").read_bytes() == content
    assert (target / "data/empty.db").read_bytes() == b""
    assert (target / "data/after.db").read_bytes() == b"after large member"
    assert (target / "data/native-checkpoint.json").is_file()
