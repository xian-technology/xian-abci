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


def test_auxiliary_tar_header_is_bounded_before_parsing(stores):
    _, target, _, original, *_ = stores
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("pax")
        member.type = tarfile.XHDTYPE
        member.size = 131072
        prefix = b"131072 comment="
        archive.addfile(member, io.BytesIO(prefix + b"x" * (member.size - len(prefix) - 1) + b"\n"))
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
