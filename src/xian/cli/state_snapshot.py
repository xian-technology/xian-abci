from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path

from xian.constants import Constants
from xian.services.state_sync import StateSnapshotManager
from xian.utils.cometbft import load_genesis_data


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="List, export, or import application state snapshots")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument(
        "--output-path",
        type=str,
        help="Target .tar.gz file for the exported snapshot",
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output file",
    )

    import_cmd = subparsers.add_parser("import")
    import_cmd.add_argument(
        "--input-path",
        type=str,
        required=True,
        help="Source .tar.gz snapshot file to import",
    )

    subparsers.add_parser("list")
    return parser


def _build_manager() -> StateSnapshotManager:
    constants = Constants()
    genesis = load_genesis_data(constants)
    return StateSnapshotManager(
        storage_home=constants.STORAGE_HOME,
        chain_id=genesis["chain_id"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = _build_manager()

    if args.command == "export":
        result = manager.export_snapshot(
            output_path=(
                Path(args.output_path).expanduser().resolve() if args.output_path else None
            ),
            force=args.force,
        )
        print(
            "State snapshot exported: "
            f"output_path={result['output_path']} "
            f"height={result['height']} "
            f"chunks={result['chunks']}"
        )
        return 0

    if args.command == "import":
        result = manager.import_snapshot_archive(Path(args.input_path).expanduser().resolve())
        print(
            "State snapshot imported: "
            f"snapshot_path={result['snapshot_path']} "
            f"stored_snapshot_path={result['stored_snapshot_path']} "
            f"height={result['height']}"
        )
        return 0

    records = [
        {
            "path": str(record.path),
            "height": record.height,
            "format": record.format,
            "chunks": record.chunks,
            "app_hash": record.app_hash_hex,
            "created_at": record.created_at,
        }
        for record in manager.list_snapshot_records()
    ]
    print(json.dumps(records, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
