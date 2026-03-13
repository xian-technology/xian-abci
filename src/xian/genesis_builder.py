from __future__ import annotations

import hashlib
import json
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracting.client import ContractingClient
from contracting.storage.driver import Driver
from contracting.storage.encoder import encode
from xian_py.wallet import Wallet

from xian.config_paths import resolve_legacy_contracts_dir
from xian.utils.block import is_compiled_key

TEMPLATE_ARG_PATTERN = re.compile(r"%%(.*?)%%")
DEFAULT_CONSENSUS_PARAMS = {
    "block": {
        "max_bytes": "22020096",
        "max_gas": "-1",
        "time_iota_ms": "1000",
    },
    "evidence": {
        "max_age_num_blocks": "100000",
        "max_age_duration": "172800000000000",
        "max_bytes": "1048576",
    },
    "validator": {
        "pub_key_types": ["ed25519"],
    },
    "version": {},
}


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
    if contracts_dir is not None:
        return contracts_dir.resolve()
    return resolve_legacy_contracts_dir()


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
    constructor_overrides: dict[str, dict[str, Any]] | None = None,
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
        override_args = (
            constructor_overrides.get(contract_name)
            or constructor_overrides.get(contract["name"])
            if constructor_overrides is not None
            else None
        )
        if override_args:
            if constructor_args is None:
                constructor_args = {}
            constructor_args.update(override_args)

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
    constructor_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_contracts_dir = resolve_contracts_dir(contracts_dir)

    if storage_home is not None:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=storage_home.resolve(),
            constructor_overrides=constructor_overrides,
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        return _build_genesis_block(
            founder_private_key=founder_private_key,
            network=network,
            contracts_dir=resolved_contracts_dir,
            storage_home=Path(tmp_dir),
            constructor_overrides=constructor_overrides,
        )


def build_validator_genesis_entry(
    *,
    priv_validator_key: dict[str, Any],
    power: int | str = 10,
    name: str = "",
) -> dict[str, Any]:
    return {
        "address": priv_validator_key["address"],
        "pub_key": priv_validator_key["pub_key"],
        "power": str(power),
        "name": name,
    }


def build_cometbft_genesis(
    *,
    chain_id: str,
    abci_genesis: dict[str, Any],
    validators: list[dict[str, Any]] | None = None,
    genesis_time: str | None = None,
) -> dict[str, Any]:
    resolved_genesis_time = genesis_time
    if resolved_genesis_time is None:
        resolved_genesis_time = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    return {
        "genesis_time": resolved_genesis_time,
        "chain_id": chain_id,
        "initial_height": str(abci_genesis.get("number", "0")),
        "consensus_params": deepcopy(DEFAULT_CONSENSUS_PARAMS),
        "validators": validators or [],
        "app_hash": "",
        "abci_genesis": abci_genesis,
    }


def build_single_validator_genesis(
    *,
    chain_id: str,
    priv_validator_key: dict[str, Any],
    founder_private_key: str,
    network: str = "local",
    validator_name: str = "",
    validator_power: int | str = 10,
    registration_fee: int = 100000,
    contracts_dir: Path | None = None,
) -> dict[str, Any]:
    founder_wallet = Wallet(private_key=founder_private_key)
    return build_local_network_genesis(
        chain_id=chain_id,
        founder_private_key=founder_private_key,
        validators=[
            {
                "account_public_key": founder_wallet.public_key,
                "name": validator_name,
                "power": validator_power,
                "priv_validator_key": priv_validator_key,
            }
        ],
        network=network,
        registration_fee=registration_fee,
        contracts_dir=contracts_dir,
    )


def build_local_network_genesis(
    *,
    chain_id: str,
    founder_private_key: str,
    validators: list[dict[str, Any]],
    network: str = "local",
    registration_fee: int = 100000,
    contracts_dir: Path | None = None,
) -> dict[str, Any]:
    if not validators:
        raise ValueError("at least one validator is required")

    founder_wallet = Wallet(private_key=founder_private_key)
    genesis_nodes = [
        validator["account_public_key"] for validator in validators
    ]
    abci_genesis = build_genesis_block(
        founder_private_key=founder_private_key,
        network=network,
        contracts_dir=contracts_dir,
        constructor_overrides={
            "currency": {"vk": founder_wallet.public_key},
            "foundation": {"vk": founder_wallet.public_key},
            "members": {
                "genesis_nodes": genesis_nodes,
                "genesis_registration_fee": registration_fee,
            },
            "masternodes": {
                "genesis_nodes": genesis_nodes,
                "genesis_registration_fee": registration_fee,
            },
        },
    )
    validator_entries = [
        build_validator_genesis_entry(
            priv_validator_key=validator["priv_validator_key"],
            power=validator.get("power", 10),
            name=validator.get("name", ""),
        )
        for validator in validators
    ]
    return build_cometbft_genesis(
        chain_id=chain_id,
        abci_genesis=abci_genesis,
        validators=validator_entries,
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
