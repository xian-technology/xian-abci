from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from xian.cli import bds_snapshot, reindex_bds, state_snapshot


@dataclass(frozen=True)
class _SnapshotRecord:
    path: Path
    height: int
    format: int
    chunks: int
    app_hash_hex: str
    created_at: str


class _StateSnapshotManager:
    def __init__(self) -> None:
        self.export_calls = []
        self.import_calls = []

    def export_snapshot(self, *, output_path=None, force=False):
        self.export_calls.append({"output_path": output_path, "force": force})
        return {"output_path": str(output_path), "height": 42, "chunks": 3}

    def import_snapshot_archive(self, snapshot_path: Path):
        self.import_calls.append(snapshot_path)
        return {
            "snapshot_path": str(snapshot_path),
            "stored_snapshot_path": "/tmp/stored-snapshot.tar.gz",
            "height": 42,
        }

    def list_snapshot_records(self):
        return [
            _SnapshotRecord(
                path=Path("/tmp/snapshot.tar.gz"),
                height=42,
                format=1,
                chunks=3,
                app_hash_hex="ab" * 32,
                created_at="2026-04-23T00:00:00Z",
            )
        ]


def test_state_snapshot_cli_export_import_and_list(
    monkeypatch, capsys, tmp_path
):
    manager = _StateSnapshotManager()
    monkeypatch.setattr(state_snapshot, "_build_manager", lambda: manager)

    output_path = tmp_path / "state.tar.gz"
    assert (
        state_snapshot.main(
            ["export", "--output-path", str(output_path), "--force"]
        )
        == 0
    )
    assert manager.export_calls == [
        {"output_path": output_path.resolve(), "force": True}
    ]
    assert "State snapshot exported:" in capsys.readouterr().out

    input_path = tmp_path / "input.tar.gz"
    assert state_snapshot.main(["import", "--input-path", str(input_path)]) == 0
    assert manager.import_calls == [input_path.resolve()]
    assert "State snapshot imported:" in capsys.readouterr().out

    assert state_snapshot.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert '"height": 42' in listed
    assert '"app_hash": "' in listed


def test_bds_snapshot_cli_export_and_import(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_export(*, output_path, force):
        calls.append(("export", output_path, force))
        return {"output_path": "/tmp/bds.tar.gz", "indexed_height": 9}

    async def fake_import(*, input_path, clear_spool):
        calls.append(("import", input_path, clear_spool))
        return {"snapshot_path": "/tmp/bds.tar.gz", "indexed_height": 9}

    monkeypatch.setattr(bds_snapshot, "_run_export", fake_export)
    monkeypatch.setattr(bds_snapshot, "_run_import", fake_import)

    output_path = tmp_path / "bds.tar.gz"
    assert (
        bds_snapshot.main(
            ["export", "--output-path", str(output_path), "--force"]
        )
        == 0
    )
    assert calls[-1] == ("export", str(output_path), True)
    assert "BDS snapshot exported:" in capsys.readouterr().out

    input_path = tmp_path / "bds-in.tar.gz"
    assert (
        bds_snapshot.main(
            ["import", "--input-path", str(input_path), "--clear-spool"]
        )
        == 0
    )
    assert calls[-1] == ("import", str(input_path), True)
    assert "BDS snapshot imported:" in capsys.readouterr().out


def test_reindex_bds_cli_passes_requested_plan(monkeypatch, capsys):
    calls = []

    async def fake_run_bds_reindex(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            indexed_height=7,
            start_height=3,
            end_height=7,
            latest_height=8,
        )

    monkeypatch.setattr(reindex_bds, "Constants", lambda: "constants")
    monkeypatch.setattr(reindex_bds, "run_bds_reindex", fake_run_bds_reindex)

    assert (
        reindex_bds.main(
            [
                "--rpc-url",
                "http://node:26657",
                "--start-height",
                "3",
                "--end-height",
                "7",
                "--reset",
            ]
        )
        == 0
    )

    assert calls == [
        {
            "constants": "constants",
            "rpc_url": "http://node:26657",
            "start_height": 3,
            "end_height": 7,
            "reset": True,
        }
    ]
    assert "BDS reindex complete:" in capsys.readouterr().out
