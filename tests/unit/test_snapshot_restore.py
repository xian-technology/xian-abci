"""Archive restoration must preserve local identity and fail without data loss."""

import fcntl
import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from xian.node_admin import apply_snapshot_archive


def archive_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type, member.linkname = value
                archive.addfile(member)
            else:
                payload = value.encode()
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def restore(home, entries):
    payload = archive_bytes(entries)
    with patch("xian.node_admin._download_binary_url", return_value=payload):
        return apply_snapshot_archive(
            "https://example.invalid/state.tar.gz",
            home,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


@pytest.fixture
def home(tmp_path):
    for name, value in {
        "config/priv_validator_key.json": "local validator key",
        "config/node_key.json": "local node key",
        "data/priv_validator_state.json": "height123",
        "data/old": "old block data",
        "xian/old": "old application data",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(value)
    return tmp_path


def assert_original(home):
    assert (home / "config/priv_validator_key.json").read_text() == "local validator key"
    assert (home / "data/priv_validator_state.json").read_text() == "height123"
    assert (home / "data/old").read_text() == "old block data"
    assert (home / "xian/old").read_text() == "old application data"


def test_restores_only_application_data_and_preserves_identity(home):
    restore(
        home,
        [
            ("config/priv_validator_key.json", "archive key"),
            ("config/node_key.json", "archive node"),
            ("data/priv_validator_state.json", "height1"),
            ("./data/new", "new blocks"),
            ("xian/new", "new application"),
        ],
    )
    assert (home / "data/new").read_text() == "new blocks"
    assert (home / "xian/new").read_text() == "new application"
    assert not (home / "data/old").exists()
    assert not (home / "xian/old").exists()
    assert (home / "data/priv_validator_state.json").read_text() == "height123"
    assert (home / "config/priv_validator_key.json").read_text() == "local validator key"
    assert (home / "config/node_key.json").read_text() == "local node key"


@pytest.mark.parametrize(
    "entries",
    [
        [("data/../config/priv_validator_key.json", "archive key")],
        [("../outside", "outside")],
        [("/data/absolute", "absolute")],
        [("data/link", (tarfile.SYMTYPE, "../config")), ("data/link/node_key.json", "bad")],
        [("data/link", (tarfile.LNKTYPE, "config/node_key.json")), ("data/link", "bad")],
        [("data/pipe", (tarfile.FIFOTYPE, ""))],
        [("data/new", "first"), ("data/new", "second")],
        [("data", "not a directory")],
    ],
)
def test_rejected_archive_keeps_original_data(home, entries):
    with pytest.raises(ValueError):
        restore(home, entries)
    assert_original(home)


@pytest.mark.parametrize("absolute", [False, True])
def test_preserves_configured_identity_paths_inside_application_roots(home, absolute):
    paths = {
        "priv_validator_state_file": "data/custom_state.json",
        "priv_validator_key_file": "xian/custom_key.json",
        "node_key_file": "data/custom_node.json",
    }
    config = []
    for key, relative in paths.items():
        configured = str(home / relative) if absolute else relative
        config.append(f'{key} = "{configured}"')
        (home / relative).write_text("local " + key)
    (home / "config/config.toml").write_text("\n".join(config))
    restore(home, [(p, "archive") for p in paths.values()] + [("data/new", "blocks")])
    for key, relative in paths.items():
        assert (home / relative).read_text() == "local " + key
    assert (home / "data/priv_validator_state.json").read_text() == "height123"


def test_missing_local_signing_state_is_not_imported(home):
    (home / "data/priv_validator_state.json").unlink()
    restore(home, [("data/priv_validator_state.json", "height1"), ("data/new", "blocks")])
    assert not (home / "data/priv_validator_state.json").exists()


def test_archive_cannot_create_children_under_protected_file(home):
    with pytest.raises(ValueError):
        restore(home, [("data/priv_validator_state.json/child", "bad")])
    assert_original(home)


def test_restore_rejects_symlinked_application_root_without_touching_target(home, tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    (target / "old").write_text("external")
    (home / "xian/old").unlink()
    (home / "xian").rmdir()
    (home / "xian").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        restore(home, [("data/new", "blocks")])
    assert (target / "old").read_text() == "external"
    assert (home / "data/old").read_text() == "old block data"


def test_publication_failure_restores_both_original_directories(home):
    rename = Path.rename

    def fail_second_directory(path, target):
        if path.name == "xian" and path.parent.name == "staged":
            raise OSError("simulated publication failure")
        return rename(path, target)

    with patch.object(Path, "rename", fail_second_directory):
        with pytest.raises(OSError, match="simulated publication failure"):
            restore(home, [("data/new", "blocks"), ("xian/new", "application")])
    assert_original(home)


def test_extraction_failure_keeps_original_directories(home):
    with patch("tarfile.TarFile.extractfile", side_effect=OSError("simulated read failure")):
        with pytest.raises(OSError, match="simulated read failure"):
            restore(home, [("data/new", "blocks")])
    assert_original(home)


def test_restore_keeps_recovery_copy_if_publication_and_rollback_fail(home):
    rename = Path.rename

    def fail_publication_and_rollback(path, target):
        if path.parent.name == "staged" and path.name == "xian":
            raise OSError("publication failed")
        if path.parent.name == "backup" and path.name == "data":
            raise OSError("rollback failed")
        return rename(path, target)

    with patch.object(Path, "rename", fail_publication_and_rollback):
        with pytest.raises(RuntimeError, match="recover original data"):
            restore(home, [("data/new", "blocks"), ("xian/new", "app")])
    recovery = list(home.glob(".snapshot-restore-*/backup/data"))
    assert len(recovery) == 1
    assert (recovery[0] / "priv_validator_state.json").read_text() == "height123"
    assert (recovery[0] / "old").read_text() == "old block data"
    with pytest.raises(ValueError, match="requires recovery"):
        restore(home, [("data/new", "blocks")])
    assert recovery[0].exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("config/ignored", "config")],
        [("data/priv_validator_state.json", (tarfile.DIRTYPE, ""))],
        [("data/identity", "file")],
    ],
)
def test_invalid_application_layout_leaves_original_data(home, entries):
    (home / "config/config.toml").write_text('node_key_file = "data/identity/node.json"')
    with pytest.raises(ValueError):
        restore(home, entries)
    assert_original(home)


def test_duplicate_directories_and_unrelated_members_are_supported(home):
    restore(
        home,
        [
            (".", (tarfile.DIRTYPE, "")),
            ("data", (tarfile.DIRTYPE, "")),
            ("./data", (tarfile.DIRTYPE, "")),
            ("data/nested", (tarfile.DIRTYPE, "")),
            ("data/nested/new", "blocks"),
            ("notes.txt", "ignored"),
        ],
    )
    assert (home / "data/nested/new").read_text() == "blocks"
    assert not (home / "notes.txt").exists()


def test_missing_identity_is_not_imported_through_case_alias(home):
    (home / "data/priv_validator_state.json").unlink()
    restore(home, [("data/PRIV_VALIDATOR_STATE.JSON", "height1"), ("data/new", "blocks")])
    assert not (home / "data/PRIV_VALIDATOR_STATE.JSON").exists()


def test_configured_external_state_is_untouched(home, tmp_path):
    external = tmp_path.parent / (tmp_path.name + "-signing-state")
    external.write_text("height123")
    try:
        (home / "config/config.toml").write_text(f'priv_validator_state_file = "{external}"')
        restore(home, [("data/new", "blocks")])
        assert external.read_text() == "height123"
    finally:
        external.unlink()


@pytest.mark.parametrize("configured", ["data", "data/subdir"])
def test_configured_identity_directory_is_rejected(home, configured):
    (home / configured).mkdir(exist_ok=True)
    (home / "config/config.toml").write_text(f'priv_validator_state_file = "{configured}"')
    with pytest.raises(ValueError, match="must be a file"):
        restore(home, [("data/new", "blocks")])
    assert_original(home)


def test_regular_file_application_root_is_rejected(home):
    (home / "xian/old").unlink()
    (home / "xian").rmdir()
    (home / "xian").write_text("keep me")
    with pytest.raises(ValueError, match="must be a directory"):
        restore(home, [("data/new", "blocks")])
    assert (home / "xian").read_text() == "keep me"
    assert (home / "data/priv_validator_state.json").read_text() == "height123"


def test_concurrent_restore_is_rejected_before_changing_data(home):
    with (home / ".snapshot-restore.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already running"):
            restore(home, [("data/new", "blocks")])
    assert_original(home)


def test_deep_archive_directories_and_configured_identity_are_preserved(home):
    identity = home / "data/local/identity/state.json"
    identity.parent.mkdir(parents=True)
    identity.write_text("height123")
    identity.chmod(0o600)
    (home / "config/config.toml").write_text(
        'priv_validator_state_file = "data/local/identity/state.json"'
    )
    restore(
        home,
        [
            ("data/a/b/c", (tarfile.DIRTYPE, "")),
            ("xian/a/b/c/value", "application"),
        ],
    )
    assert identity.read_text() == "height123"
    assert identity.stat().st_mode & 0o777 == 0o600
    assert (home / "xian/a/b/c/value").read_text() == "application"


def test_empty_leftover_staging_directory_does_not_block_restore(home):
    (home / ".snapshot-restore-empty/backup").mkdir(parents=True)
    (home / ".snapshot-restore-no-backup").mkdir()
    restore(home, [("data/new", "blocks")])
    assert (home / "data/new").read_text() == "blocks"


@pytest.mark.parametrize(
    "entries",
    [
        [("xian", "file")],
        [("data/value", (tarfile.DIRTYPE, "")), ("data/value", "file")],
        [("data/value", "file"), ("data/value", (tarfile.DIRTYPE, ""))],
    ],
)
def test_directory_file_conflicts_are_rejected(home, entries):
    with pytest.raises(ValueError):
        restore(home, entries)
    assert_original(home)


def test_missing_application_root_cannot_be_configured_as_identity_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.toml").write_text('priv_validator_state_file = "data"')
    with pytest.raises(ValueError, match="must be a file"):
        restore(tmp_path, [("data/new", "blocks")])
    assert not (tmp_path / "data").exists()


def test_configured_case_alias_is_preserved_on_case_insensitive_filesystem(home):
    if not (home / "DATA").exists():
        pytest.skip("filesystem is case-sensitive")
    state = home / "data/custom.json"
    state.write_text("height123")
    (home / "config/config.toml").write_text('priv_validator_state_file = "DATA/custom.json"')
    restore(home, [("data/custom.json", "height1"), ("data/new", "blocks")])
    assert state.read_text() == "height123"


def test_missing_configured_identity_is_not_imported_through_unicode_alias(home):
    (home / "config/config.toml").write_text('priv_validator_state_file = "data/état.json"')
    restore(home, [("data/e\u0301tat.json", "height1"), ("data/new", "blocks")])
    assert not (home / "data/e\u0301tat.json").exists()


def test_configured_symlinked_identity_is_rejected(home):
    (home / "data/custom.json").symlink_to("priv_validator_state.json")
    (home / "config/config.toml").write_text('priv_validator_state_file = "data/custom.json"')
    with pytest.raises(ValueError, match="symlinked"):
        restore(home, [("data/new", "blocks")])
    assert_original(home)


def test_config_identity_symlink_keeps_its_target_inside_restored_data(home):
    target = home / "data/custom_key.json"
    target.write_text("local validator key")
    key = home / "config/priv_validator_key.json"
    key.unlink()
    key.symlink_to("../data/custom_key.json")
    restore(home, [("data/custom_key.json", "archive key"), ("data/new", "blocks")])
    assert key.is_symlink()
    assert key.read_text() == "local validator key"


def test_configured_path_through_external_home_alias_is_preserved(home, tmp_path):
    alias = tmp_path.parent / (tmp_path.name + "-home-alias")
    alias.symlink_to(home, target_is_directory=True)
    try:
        (home / "data/custom.json").write_text("height123")
        (home / "config/config.toml").write_text(
            f'priv_validator_state_file = "{alias}/data/custom.json"'
        )
        restore(home, [("data/custom.json", "height1"), ("data/new", "blocks")])
        assert (alias / "data/custom.json").read_text() == "height123"
    finally:
        alias.unlink()
