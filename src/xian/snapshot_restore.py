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


def _application_members(archive: tarfile.TarFile, protected: set[Path]):
    protected_names = {_path_key(path) for path in protected}
    seen: dict[str, tarfile.TarInfo] = {}
    selected = []
    for member in archive.getmembers():
        path = _member_path(member)
        if not path.parts or path.parts[0] not in _ROOTS:
            continue
        if len(path.parts) == 1 and not member.isdir():
            raise ValueError(f"snapshot application root must be a directory: {path}")
        if _is_protected_member(path, member, protected_names):
            continue
        if _new_member(path, member, seen):
            selected.append((member, path))
    if not any(member.isfile() for member, _ in selected):
        raise ValueError("snapshot archive contains no application data")
    return selected


def _extract_application_data(archive_path: Path, staged: Path, protected: set[Path]) -> None:
    with tarfile.open(archive_path) as archive:
        members = _application_members(archive, protected)
        for member, relative in members:
            destination = staged / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)


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
    for previous in home.glob(_WORK_PREFIX + "*"):
        backup = previous / "backup"
        if backup.exists() and any(backup.iterdir()):
            raise ValueError(f"incomplete snapshot restore requires recovery from {backup}")
    for name in _ROOTS:
        path = home / name
        _check_plain_path(path, home)
        if path.exists() and not path.is_dir():
            raise ValueError(f"snapshot application root must be a directory: {path}")


def _restore_application_archive(archive_path: Path, home: Path) -> None:
    _check_home(home)
    protected = _protected_paths(home)
    workspace = Path(tempfile.mkdtemp(prefix=_WORK_PREFIX, dir=home))
    staged, backup = workspace / "staged", workspace / "backup"
    backup.mkdir()
    for name in _ROOTS:
        (staged / name).mkdir(parents=True)
    try:
        _extract_application_data(archive_path, staged, protected)
        _preserve_identity(home, staged, protected)
        _publish(home, staged, backup)
    except BaseException:
        # A failed rollback must never erase the remaining recovery copy.
        if not any(backup.iterdir()):
            shutil.rmtree(workspace)
        raise
    else:
        shutil.rmtree(workspace)


def restore_application_archive(archive_path: Path, home: Path) -> None:
    home = home.resolve()
    with (home / ".snapshot-restore.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another snapshot restore is already running") from error
        _restore_application_archive(archive_path, home)
