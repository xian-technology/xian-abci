"""Crash recovery barrier shared by restore, node startup and backup export."""

from pathlib import Path


def validate_snapshot_restore_complete(home: Path) -> None:
    for previous in Path(home).glob(".snapshot-restore-*"):
        backup = previous / "backup"
        if (previous / "publication-started").exists() or (
            backup.exists() and any(backup.iterdir())
        ):
            raise ValueError(f"incomplete snapshot restore requires recovery from {backup}")
