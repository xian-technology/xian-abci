import binascii
import json
import marshal
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from xian_runtime_types.encoding import convert_dict

from xian.constants import Constants as c


def _latest_block_path(storage_home: Path | None = None) -> Path:
    resolved_storage_home = (
        Path(storage_home) if storage_home is not None else c.STORAGE_HOME
    )
    return resolved_storage_home / "__latest_block.json"


def nanoseconds_to_utc_datetime(nanoseconds: int) -> datetime:
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    microseconds = remainder // 1_000
    return datetime.fromtimestamp(seconds, UTC).replace(
        microsecond=microseconds
    )


def convert_cometbft_time_to_datetime(nanoseconds: int) -> datetime:
    return nanoseconds_to_utc_datetime(nanoseconds)


def get_nanotime_from_block_time(timeobj) -> int:
    seconds = timeobj.seconds
    nanos = timeobj.nanos
    return (seconds * 1_000_000_000) + nanos


def compile_contract_from_source(s: dict):
    code = compile(s["value"], "", "exec")
    serialized_code = marshal.dumps(code)
    hexadecimal_string = binascii.hexlify(serialized_code).decode()
    return hexadecimal_string


def apply_state_changes_from_block(client, nonce_storage, block):
    state_changes = block.get("genesis", [])
    rewards = block.get("rewards", [])

    nanos = block.get("hlc_timestamp")
    nonces = block.get("nonces", [])

    for i, s in enumerate(state_changes):
        parts = s["key"].split(".")

        if parts[1] == "__code__":
            logger.info(f"Processing contract: {parts[0]}")
            compiled_code = compile_contract_from_source(s)
            client.raw_driver.set(f"{parts[0]}.__compiled__", compiled_code)
        if type(s["value"]) is dict:
            s["value"] = convert_dict(s["value"])

        client.raw_driver.set(s["key"], s["value"])

    for n in nonces:
        nonce_storage.set_nonce(n["key"], n["value"])

    for s in rewards:
        if type(s["value"]) is dict:
            s["value"] = convert_dict(s["value"])

        client.raw_driver.set(s["key"], s["value"])

    client.raw_driver.hard_apply(nanos)


async def store_genesis_block(client, nonce_storage, genesis_block: dict):
    if genesis_block is not None:
        apply_state_changes_from_block(client, nonce_storage, genesis_block)


def is_compiled_key(key):
    parts = key.split(".")
    if parts[1] == "__compiled__":
        return True
    return False


def create_latest_block_json_if_not_exists(
    storage_home: Path | None = None,
):
    latest_block_path = _latest_block_path(storage_home)
    latest_block_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(latest_block_path, "x") as f:
            json.dump({"hash": "", "height": 0, "nanos": 0}, f)
    except FileExistsError:
        pass


def get_latest_block_hash(storage_home: Path | None = None):
    # Get the latest block hash from the json file
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)
            latest_hash = bytes.fromhex(latest_block.get("hash"))
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")

    return latest_hash


def set_latest_block_hash(h, storage_home: Path | None = None):
    # Set the latest block hash in the json file
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)

        # Update the hash while keeping the height intact
        latest_block["hash"] = h.hex()

        with open(latest_block_path, "w") as f:
            json.dump(latest_block, f)
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")


def get_latest_block_height(storage_home: Path | None = None):
    # Get the latest block height from the json file
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)
            latest_height = latest_block.get("height")
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")

    return latest_height


def get_latest_block_nanos(storage_home: Path | None = None):
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)
            latest_nanos = latest_block.get("nanos", 0)
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")

    return int(latest_nanos or 0)


def set_latest_block_height(h, storage_home: Path | None = None):
    # Set the latest block height in the json file
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)

        # Update the height while keeping the hash intact
        latest_block["height"] = h

        with open(latest_block_path, "w") as f:
            json.dump(latest_block, f)
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")


def set_latest_block_nanos(nanos, storage_home: Path | None = None):
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    try:
        with open(latest_block_path, "r") as f:
            latest_block = json.load(f)

        latest_block["nanos"] = int(nanos)

        with open(latest_block_path, "w") as f:
            json.dump(latest_block, f)
    except FileNotFoundError:
        raise Exception("__latest_block.json not found")
    except json.JSONDecodeError:
        raise Exception("Error decoding __latest_block.json")
