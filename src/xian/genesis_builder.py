from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from contracting.client import ContractingClient
from contracting.storage.driver import Driver
from contracting.storage.encoder import encode
from xian_py.wallet import Wallet

from xian.utils.block import is_compiled_key

DEFAULT_CONTRACTS_DIR = (
    Path(__file__).resolve().parent / "tools" / "genesis" / "contracts"
)
TEMPLATE_ARG_PATTERN = re.compile(r"%%(.*?)%%")


def hash_block_data(
    hlc_timestamp: str, block_number: str, previous_block_hash: str
) -> str:
    digest = hashlib.sha3_256()
    digest.update(
        f"{hlc_timestamp}{block_number}{previous_block_hash}".encode()
    )
    return digest.hexdigest()


def hash_state_changes(state_changes: list[dict[str, Any]]) -> str:
    ordered_state_changes = sorted(state_changes, key=lambda item: item["key"])
    digest = hashlib.sha3_256()
    digest.update(encode(ordered_state_changes).encode("utf-8"))
    return digest.hexdigest()


def resolve_contracts_dir(contracts_dir: Path | None = None) -> Path:
    return (
        contracts_dir.resolve()
        if contracts_dir is not None
        else DEFAULT_CONTRACTS_DIR.resolve()
    )


def render_template_values(value: Any, substitutions: dict[str, str]) -> Any:
    if isinstance(value, str):
        match = TEMPLATE_ARG_PATTERN.search(value)
        if match is None:
            return value
        replacement = substitutions[match.group(1)]
        return value.replace(match.group(0), replacement)

    if isinstance(value, list):
        return [render_template_values(item, substitutions) for item in value]

    if isinstance(value, dict):
        return {
            key: render_template_values(item, substitutions)
            for key, item in value.items()
        }

    return value


def _build_genesis_block(
    *,
    founder_private_key: str,
    network: str,
    contracts_dir: Path,
    storage_home: Path,
) -> dict[str, Any]:
    contracting = ContractingClient(driver=Driver(storage_home=storage_home))
    contracting.set_submission_contract(commit=False)

    config_path = contracts_dir / f"contracts_{network}.json"
    with open(config_path, "r", encoding="utf-8") as handle:
        contract_config = json.load(handle)

    wallet = Wallet(private_key=founder_private_key)
    substitutions = {
        "founder_privkey": founder_private_key,
        "founder_private_key": founder_private_key,
        "founder_public_key": wallet.public_key,
    }
    extension = contract_config["extension"]

    for contract in contract_config["contracts"]:
        contract_name = render_template_values(contract["name"], substitutions)
        submit_as = contract.get("submit_as")
        if submit_as is not None:
            contract_name = render_template_values(submit_as, substitutions)

        contract_path = contracts_dir / (
            render_template_values(contract["name"], substitutions) + extension
        )
        code = contract_path.read_text(encoding="utf-8")
        owner = render_template_values(contract.get("owner"), substitutions)
        constructor_args = render_template_values(
            contract.get("constructor_args"), substitutions
        )

        if contracting.get_contract(contract_name) is None:
            contracting.submit(
                code,
                name=contract_name,
                owner=owner,
                constructor_args=constructor_args,
            )

    genesis_block = {
        "hash": hash_block_data(
            "0000-00-00T00:00:00.000000000Z_0",
            "0",
            "0" * 64,
        ),
        "number": "0",
        "genesis": [],
        "origin": {
            "signature": "",
            "sender": "",
        },
    }

    for key, value in contracting.raw_driver.pending_writes.items():
        if value is None or is_compiled_key(key):
            continue
        genesis_block["genesis"].append({"key": key, "value": value})

    genesis_block["origin"]["sender"] = wallet.public_key
    genesis_block["origin"]["signature"] = wallet.sign_msg(
        hash_state_changes(genesis_block["genesis"])
    )
    return genesis_block


def build_genesis_block(
    *,
    founder_private_key: str,
    network: str = "devnet",
    contracts_dir: Path | None = None,
    storage_home: Path | None = None,
) -> dict[str, Any]:
    resolved_contracts_dir = resolve_contracts_dir(contracts_dir)

    if storage_home is not None:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=storage_home.resolve(),
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=Path(tmp_dir),
        )


def write_genesis_block(path: Path, genesis_block: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(encode(genesis_block))


def update_cometbft_genesis(
    genesis_path: Path, *, abci_genesis: dict[str, Any]
) -> None:
    with open(genesis_path, "r", encoding="utf-8") as handle:
        cometbft_genesis = json.load(handle)

    cometbft_genesis["abci_genesis"] = abci_genesis
    cometbft_genesis["initial_height"] = str(abci_genesis["number"])

    with open(genesis_path, "w", encoding="utf-8") as handle:
        handle.write(encode(cometbft_genesis))
