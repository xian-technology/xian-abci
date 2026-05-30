from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from pathlib import Path

from xian.constants import Constants
from xian.services.bds.bds import BDS
from xian.services.bds.reindex import CometBftRpcClient, resolve_rpc_url
from xian.services.bds.runtime import resolve_bds_config
from xian.services.bds.snapshot import (
    default_snapshot_output_path,
    export_bds_snapshot,
    import_bds_snapshot,
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Export or import BDS snapshots for fast bootstrap")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument(
        "--output-path",
        type=str,
        help="Target .tar.gz file for the exported BDS snapshot",
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
    import_cmd.add_argument(
        "--clear-spool",
        action="store_true",
        help="Delete all local spool files before importing the snapshot",
    )
    return parser


async def _run_export(*, output_path: str | None, force: bool) -> dict:
    constants = Constants()
    bds = BDS(config=resolve_bds_config(constants))
    try:
        await bds.open_storage()
        await bds.ensure_schema()
        if output_path is None:
            status = await bds.get_status()
            resolved_output = default_snapshot_output_path(
                output_dir=Path.cwd(),
                indexed_height=status["indexed"]["indexed_height"],
            )
        else:
            resolved_output = Path(output_path).expanduser().resolve()
        return await export_bds_snapshot(
            bds=bds,
            output_path=resolved_output,
            force=force,
        )
    finally:
        await bds.close()


async def _run_import(*, input_path: str, clear_spool: bool) -> dict:
    constants = Constants()
    bds = BDS(config=resolve_bds_config(constants))
    trusted_block_source = CometBftRpcClient(resolve_rpc_url(constants))
    try:
        await bds.open_storage()
        return await import_bds_snapshot(
            bds=bds,
            snapshot_path=Path(input_path).expanduser().resolve(),
            clear_spool=clear_spool,
            trusted_block_source=trusted_block_source,
        )
    finally:
        await trusted_block_source.close()
        await bds.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "export":
        result = asyncio.run(_run_export(output_path=args.output_path, force=args.force))
        print(
            "BDS snapshot exported: "
            f"output_path={result['output_path']} "
            f"indexed_height={result['indexed_height']}"
        )
    else:
        result = asyncio.run(
            _run_import(
                input_path=args.input_path,
                clear_spool=args.clear_spool,
            )
        )
        print(
            "BDS snapshot imported: "
            f"snapshot_path={result['snapshot_path']} "
            f"indexed_height={result['indexed_height']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
