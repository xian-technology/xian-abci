"""Restore application archives without importing node identity or signing state.

The node must be stopped. Extraction happens in a private staging directory;
directory publication rolls back on failure. An interrupted publication retains
its backup for operator recovery and blocks another restore.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tarfile
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath

from xian.execution_policy import validate_persisted_execution_policy
from xian.full_node_store import read_full_node_store_checkpoint, verify_full_node_store_checkpoint
from xian.full_node_store_limits import FullNodeStoreLimits
from xian.services.snapshot_limits import SnapshotArchiveReader, SnapshotLimits
from xian.snapshot_restore_status import validate_snapshot_restore_complete
from xian.toml_utils import load as load_toml

_ROOTS = ("data", "xian")
_IDENTITY_PATHS = {
    "priv_validator_state_file": "data/priv_validator_state.json",
    "priv_validator_key_file": "config/priv_validator_key.json",
    "node_key_file": "config/node_key.json",
}
_WORK_PREFIX = ".snapshot-restore-"


def _check_plain_path(path: Path, home: Path) -> None:
    while path != home:
        if path.is_symlink():
            raise ValueError(f"snapshot restore does not replace symlinked local paths: {path}")
        path = path.parent


def _protected_paths(home: Path) -> set[Path]:
    config_path = home / "config" / "config.toml"
    config = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            config = load_toml(handle)
    configured = {config.get(key, default) for key, default in _IDENTITY_PATHS.items()}
    protected = set()
    for value in configured | set(_IDENTITY_PATHS.values()):
        relative = _protected_relative_path(home, value)
        if relative is not None:
            protected.add(relative)
    return protected


def _protected_relative_path(home: Path, value: str) -> Path | None:
    # Config/external links survive publication, but their targets may not.
    path = _resolve_identity_path(home / value, home)
    if path.is_dir():
        raise ValueError(f"configured identity path must be a file: {path}")
    relative = _home_relative_path(path, home)
    if relative is None:
        return None
    root = _application_root(home, relative.parts[0])
    if root is None:
        return None
    _check_plain_path(home / relative, home)
    if len(relative.parts) == 1:
        raise ValueError(f"configured identity path must be a file: {path}")
    return Path(root, *relative.parts[1:])


def _resolve_identity_path(path: Path, home: Path, hops: int = 0) -> Path:
    if hops > 40:
        raise ValueError(f"too many identity path symlinks: {path}")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            _check_local_identity_path(current, home)
            target = current.parent / os.readlink(current)
            remainder = path.parts[index + 2 :]
            return _resolve_identity_path(target.joinpath(*remainder), home, hops + 1)
    return current


def _check_local_identity_path(path: Path, home: Path) -> None:
    relative = _home_relative_path(path, home)
    if relative is None:
        return
    if relative.parts and _application_root(home, relative.parts[0]) is not None:
        _check_plain_path(home / relative, home)


def _home_relative_path(path: Path, home: Path) -> Path | None:
    for parent in path.parents:
        if parent.exists() and parent.samefile(home):
            return path.relative_to(parent)
    return None


def _application_root(home: Path, name: str) -> str | None:
    if name in _ROOTS:
        return name
    candidate = home / name
    for root in _ROOTS:
        application = home / root
        if candidate.exists() and application.exists() and candidate.samefile(application):
            return root
    return None


def _path_key(path: PurePosixPath | Path) -> str:
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _member_path(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or "\\" in member.name:
        raise ValueError(f"snapshot archive contains invalid path: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"snapshot archive contains unsupported member: {member.name}")
    return path


def _is_protected_member(path: PurePosixPath, member: tarfile.TarInfo, protected: set[str]) -> bool:
    # Conservatively protect aliases on case-insensitive filesystems as well.
    key = _path_key(path)
    if key in protected:
        if member.isdir():
            raise ValueError(f"snapshot directory conflicts with local identity: {path}")
        return True
    for local in protected:
        if key.startswith(local + "/"):
            raise ValueError(f"snapshot path is inside a local identity file: {path}")
        if local.startswith(key + "/") and not member.isdir():
            raise ValueError(f"snapshot file conflicts with local identity directory: {path}")
    return False


def _new_member(path: PurePosixPath, member: tarfile.TarInfo, seen: dict) -> bool:
    key = _path_key(path)
    if key in seen:
        if member.isdir() and seen[key].isdir():
            return False
        raise ValueError(f"snapshot archive contains duplicate path: {path}")
    seen[key] = member
    return True


def _extract_member(archive, member, relative, staged, limits):
    destination = staged / relative
    if member.isdir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    limits.require_disk_space(staged, member.size)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.extractfile(member) as source, destination.open("xb") as target:
        # extractfile returns a fresh, unread BufferedReader. Its read-ahead
        # can exceed the strict auxiliary-header cap; bound payload reads on
        # its public raw stream instead, before the buffer consumes any bytes.
        shutil.copyfileobj(source.raw, target, length=16 * 1024)


def _extract_application_data(
    archive_path, staged, protected, *, limits, workspace, expected_bytes
):
    unpacked = workspace / "archive.tar"
    limits.unpack_archive(archive_path, unpacked, expected_bytes=expected_bytes)
    protected_names = {_path_key(path) for path in protected}
    seen, count, declared, files = {}, 0, 0, 0
    with unpacked.open("rb") as file:
        # Bound PAX/GNU auxiliary header reads before tarfile allocates them.
        reader = SnapshotArchiveReader(file, SnapshotLimits())
        reader.allow_payload_reads()
        with tarfile.open(fileobj=reader, mode="r:") as archive:
            for member in archive:
                count += 1
                limits.check_size(count, limits.max_members, "member count")
                if member.size < 0:
                    raise ValueError("snapshot member size must be nonnegative")
                declared += member.size
                limits.check_size(declared, limits.max_uncompressed_bytes, "declared file payloads")
                path = _selected_member(member, protected_names, seen)
                if path is not None:
                    _extract_member(archive, member, path, staged, limits)
                    files += member.isfile()
    if not files:
        raise ValueError("snapshot archive contains no application data")


def _selected_member(member, protected_names, seen):
    path = _member_path(member)
    if not path.parts or path.parts[0] not in _ROOTS:
        return None
    if len(path.parts) == 1 and not member.isdir():
        raise ValueError(f"snapshot application root must be a directory: {path}")
    if _is_protected_member(path, member, protected_names):
        return None
    return path if _new_member(path, member, seen) else None


def _preserve_identity(home: Path, staged: Path, protected: set[Path]) -> None:
    for relative in protected:
        source = home / relative
        if source.exists():
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _rollback(home: Path, backup: Path, published: list[str]) -> None:
    for name in reversed(_ROOTS):
        destination = home / name
        original = backup / name
        if name in published:
            shutil.rmtree(destination)
        if original.exists():
            original.rename(destination)


def _begin_publication(workspace):
    with (workspace / "publication-started").open("xb") as marker:
        marker.flush()
        os.fsync(marker.fileno())
    descriptor = os.open(workspace, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(home: Path, staged: Path, backup: Path) -> None:
    published = []
    try:
        for name in _ROOTS:
            destination = home / name
            if destination.exists():
                destination.rename(backup / name)
            (staged / name).rename(destination)
            published.append(name)
    except BaseException:
        try:
            _rollback(home, backup, published)
        except BaseException as error:
            raise RuntimeError(
                f"snapshot rollback failed; recover original data from {backup}"
            ) from error
        raise


def _check_home(home: Path) -> None:
    validate_snapshot_restore_complete(home)
    for name in _ROOTS:
        path = home / name
        _check_plain_path(path, home)
        if path.exists() and not path.is_dir():
            raise ValueError(f"snapshot application root must be a directory: {path}")


def _restore_application_archive(
    archive_path, home, *, manifest, native_store_inspector, limits, expected_chain_id
):
    _check_home(home)
    limits.check_size(archive_path.stat().st_size, limits.max_archive_bytes, "archive")
    validate_persisted_execution_policy(home / "xian")
    protected = _protected_paths(home)
    workspace = Path(tempfile.mkdtemp(prefix=_WORK_PREFIX, dir=home))
    staged, backup = workspace / "staged", workspace / "backup"
    backup.mkdir()
    for name in _ROOTS:
        (staged / name).mkdir(parents=True)
    try:
        _extract_application_data(
            archive_path,
            staged,
            protected,
            limits=limits,
            workspace=workspace,
            expected_bytes=manifest["uncompressed_bytes"] if manifest else None,
        )
        if not validate_persisted_execution_policy(staged / "xian"):
            raise ValueError("snapshot is missing its execution policy")
        point = _verify_staged_checkpoint(staged, home, manifest, native_store_inspector)
        if expected_chain_id is not None and point["chain_id"] != expected_chain_id:
            raise ValueError("full-node-store checkpoint chain_id mismatch")
        _preserve_identity(home, staged, protected)
        _begin_publication(workspace)
        _publish(home, staged, backup)
    except BaseException:
        # A failed rollback must never erase the remaining recovery copy.
        if not any(backup.iterdir()):
            shutil.rmtree(workspace)
        raise
    else:
        shutil.rmtree(workspace)


def _verify_staged_checkpoint(staged, home, manifest, native_store_inspector):
    kwargs = {"reference_home": home, "native_store_inspector": native_store_inspector}
    if manifest is not None:
        return verify_full_node_store_checkpoint(staged, manifest, **kwargs)
    return read_full_node_store_checkpoint(staged, **kwargs)


def restore_application_archive(
    archive_path: Path,
    home: Path,
    *,
    manifest: dict | None = None,
    native_store_inspector=None,
    limits: FullNodeStoreLimits | None = None,
    expected_chain_id: str | None = None,
) -> None:
    home = home.resolve()
    with (home / ".snapshot-restore.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another snapshot restore is already running") from error
        _restore_application_archive(
            archive_path,
            home,
            manifest=manifest,
            native_store_inspector=native_store_inspector,
            limits=limits or FullNodeStoreLimits(),
            expected_chain_id=expected_chain_id,
        )
