from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from contracting import constants as contracting_constants
from contracting.storage.driver import Driver
from xian_py.wallet import Wallet
from xian_runtime_types.encoding import decode, encode

from xian.utils.block import (
    compile_contract_from_source,
    get_latest_block_hash,
    get_latest_block_height,
    get_latest_block_nanos,
    is_compiled_key,
    set_latest_block_hash,
    set_latest_block_height,
    set_latest_block_nanos,
)


def hash_state_changes(state_changes: list[dict[str, Any]]) -> str:
    def serialize(obj: Any) -> str:
        if isinstance(obj, bytes):
            return obj.hex()
        raise TypeError(
            f"object of type {type(obj)!r} is not JSON serializable"
        )

    digest = hashlib.sha3_256()
    digest.update(json.dumps(state_changes, default=serialize).encode("utf-8"))
    return digest.hexdigest()


def fetch_filebased_state(
    *,
    storage_home: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    driver = (
        Driver(storage_home=storage_home)
        if storage_home is not None
        else Driver()
    )
    contract_state = driver.get_all_contract_state()
    run_state = driver.get_run_state()
    return contract_state, run_state


def build_exported_state(
    *,
    founder_private_key: str | None,
    contract_state: dict[str, Any],
    run_state: dict[str, Any],
    latest_block_hash: bytes | None = None,
    latest_block_height: int | None = None,
    latest_block_nanos: int | None = None,
    storage_home: Path | None = None,
) -> dict[str, Any]:
    block_hash = latest_block_hash or get_latest_block_hash(storage_home)
    block_height = (
        latest_block_height
        if latest_block_height is not None
        else get_latest_block_height(storage_home)
    )
    block_nanos = (
        latest_block_nanos
        if latest_block_nanos is not None
        else get_latest_block_nanos(storage_home)
    )

    exported_state = {
        "hash": block_hash.hex(),
        "number": block_height,
        "nanos": block_nanos,
        "origin": {
            "signature": "",
            "sender": "",
        },
        "genesis": [],
    }

    nonces = [
        {"key": key[4:], "value": value}
        for key, value in run_state.items()
        if key.startswith("__n.")
    ]
    nonces = sorted(nonces, key=lambda item: item["key"])

    for key, value in contract_state.items():
        if not is_compiled_key(key) and value is not None:
            exported_state["genesis"].append({"key": key, "value": value})

    exported_state["genesis"] = sorted(
        exported_state["genesis"],
        key=lambda item: item["key"],
    )
    exported_state["nonces"] = nonces

    if founder_private_key:
        founder_wallet = Wallet(private_key=founder_private_key)
        exported_state["origin"]["sender"] = founder_wallet.public_key
        exported_state["origin"]["signature"] = founder_wallet.sign_msg(
            hash_state_changes(exported_state["genesis"])
        )

    return exported_state


def export_state(
    *,
    output_dir: Path,
    founder_private_key: str | None = None,
    storage_home: Path | None = None,
    output_filename: str = "exported_state.json",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_state, run_state = fetch_filebased_state(storage_home=storage_home)
    exported_state = build_exported_state(
        founder_private_key=founder_private_key,
        contract_state=contract_state,
        run_state=run_state,
        storage_home=storage_home,
    )

    output_path = output_dir / output_filename
    output_path.write_text(encode(exported_state), encoding="utf-8")
    return output_path


def load_exported_state(path: Path) -> dict[str, Any]:
    return decode(path.read_text(encoding="utf-8"))


def import_state(
    *,
    exported_state: dict[str, Any],
    storage_home: Path | None = None,
) -> dict[str, Any]:
    resolved_storage_home = Path(storage_home) if storage_home else None
    driver = (
        Driver(storage_home=resolved_storage_home)
        if resolved_storage_home is not None
        else Driver()
    )
    driver.flush_full()

    writes: dict[str, Any] = {}
    for entry in exported_state.get("genesis", []):
        key = entry["key"]
        value = entry["value"]
        writes[key] = value
        if key.endswith(f"{contracting_constants.INDEX_SEPARATOR}__code__"):
            contract_name = key.split(contracting_constants.INDEX_SEPARATOR, 1)[
                0
            ]
            writes[
                (
                    f"{contract_name}{contracting_constants.INDEX_SEPARATOR}"
                    "__compiled__"
                )
            ] = compile_contract_from_source(entry)

    for nonce in exported_state.get("nonces", []):
        writes[
            (f"__n{contracting_constants.INDEX_SEPARATOR}{nonce['key']}")
        ] = nonce["value"]

    if writes:
        driver._store.batch_set(writes)
    driver.flush_cache()

    latest_block_hash = bytes.fromhex(exported_state.get("hash", ""))
    latest_block_height = int(exported_state.get("number", 0))
    latest_block_nanos = int(exported_state.get("nanos", 0))
    set_latest_block_hash(latest_block_hash, resolved_storage_home)
    set_latest_block_height(latest_block_height, resolved_storage_home)
    set_latest_block_nanos(latest_block_nanos, resolved_storage_home)

    return {
        "height": latest_block_height,
        "app_hash": latest_block_hash.hex(),
        "nanos": latest_block_nanos,
        "keys_imported": len(writes),
    }
