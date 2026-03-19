from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from contracting.storage.driver import Driver
from xian_py.wallet import Wallet
from xian_runtime_types.encoding import encode

from xian.utils.block import (
    get_latest_block_hash,
    get_latest_block_height,
    is_compiled_key,
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


def fetch_filebased_state() -> tuple[dict[str, Any], dict[str, Any]]:
    driver = Driver()
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
) -> dict[str, Any]:
    block_hash = latest_block_hash or get_latest_block_hash()
    block_height = (
        latest_block_height
        if latest_block_height is not None
        else get_latest_block_height()
    )

    exported_state = {
        "hash": block_hash.hex(),
        "number": block_height,
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
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_state, run_state = fetch_filebased_state()
    exported_state = build_exported_state(
        founder_private_key=founder_private_key,
        contract_state=contract_state,
        run_state=run_state,
    )

    output_path = output_dir / "exported_state.json"
    output_path.write_text(encode(exported_state), encoding="utf-8")
    return output_path
