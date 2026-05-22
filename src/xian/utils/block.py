import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from xian_runtime_types.encoding import convert_dict

from xian.constants import Constants as c

LATEST_BLOCK_DEFAULT = {"hash": "", "height": 0, "nanos": 0}
LATEST_BLOCK_READ_RETRIES = 8
LATEST_BLOCK_READ_RETRY_DELAY_SECONDS = 0.01


def _latest_block_path(storage_home: Path | None = None) -> Path:
    resolved_storage_home = (
        Path(storage_home) if storage_home is not None else c.STORAGE_HOME
    )
    return resolved_storage_home / "__latest_block.json"


def _normalize_latest_block(latest_block: dict) -> dict:
    return {
        "hash": str(latest_block.get("hash", "") or ""),
        "height": int(latest_block.get("height", 0) or 0),
        "nanos": int(latest_block.get("nanos", 0) or 0),
    }


def _write_latest_block_json(
    latest_block_path: Path,
    latest_block: dict,
    *,
    replace_existing: bool,
) -> None:
    latest_block_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=latest_block_path.parent,
            prefix=f".{latest_block_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(_normalize_latest_block(latest_block), temp_file)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        if replace_existing:
            os.replace(temp_path, latest_block_path)
        else:
            try:
                os.link(temp_path, latest_block_path)
            except FileExistsError:
                pass
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _load_latest_block(storage_home: Path | None = None) -> dict:
    latest_block_path = _latest_block_path(storage_home)
    create_latest_block_json_if_not_exists(storage_home)
    last_error: Exception | None = None

    for _ in range(LATEST_BLOCK_READ_RETRIES):
        try:
            with open(latest_block_path, "r", encoding="utf-8") as f:
                return _normalize_latest_block(json.load(f))
        except FileNotFoundError as exc:
            last_error = exc
            create_latest_block_json_if_not_exists(storage_home)
        except json.JSONDecodeError as exc:
            last_error = exc
        time.sleep(LATEST_BLOCK_READ_RETRY_DELAY_SECONDS)

    if isinstance(last_error, FileNotFoundError):
        raise Exception("__latest_block.json not found") from last_error
    if isinstance(last_error, json.JSONDecodeError):
        raise Exception("Error decoding __latest_block.json") from last_error
    raise Exception("Error loading __latest_block.json")


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


def _convert_runtime_value(value):
    if type(value) is dict:
        return convert_dict(value)
    if isinstance(value, list):
        return [_convert_runtime_value(item) for item in value]
    return value


def apply_state_changes_from_block(client, nonce_storage, block):
    state_changes = block.get("genesis", [])
    rewards = block.get("rewards", [])

    nanos = block.get("hlc_timestamp")
    nonces = block.get("nonces", [])

    for i, s in enumerate(state_changes):
        s["value"] = _convert_runtime_value(s["value"])
        client.raw_driver.set(s["key"], s["value"])

    for n in nonces:
        nonce_storage.set_nonce(n["key"], n["value"])

    for s in rewards:
        s["value"] = _convert_runtime_value(s["value"])
        client.raw_driver.set(s["key"], s["value"])

    client.raw_driver.hard_apply(nanos)


async def store_genesis_block(client, nonce_storage, genesis_block: dict):
    if genesis_block is not None:
        apply_state_changes_from_block(client, nonce_storage, genesis_block)


def create_latest_block_json_if_not_exists(
    storage_home: Path | None = None,
):
    latest_block_path = _latest_block_path(storage_home)
    if latest_block_path.exists():
        return
    _write_latest_block_json(
        latest_block_path,
        LATEST_BLOCK_DEFAULT,
        replace_existing=False,
    )


def get_latest_block_hash(storage_home: Path | None = None):
    # Get the latest block hash from the json file
    latest_block = _load_latest_block(storage_home)
    return bytes.fromhex(latest_block.get("hash"))


def set_latest_block(
    *,
    block_hash: bytes | None = None,
    height: int | None = None,
    nanos: int | None = None,
    storage_home: Path | None = None,
) -> None:
    latest_block_path = _latest_block_path(storage_home)
    latest_block = _load_latest_block(storage_home)

    if block_hash is not None:
        latest_block["hash"] = block_hash.hex()
    if height is not None:
        latest_block["height"] = int(height)
    if nanos is not None:
        latest_block["nanos"] = int(nanos)

    _write_latest_block_json(
        latest_block_path,
        latest_block,
        replace_existing=True,
    )


def set_latest_block_hash(h, storage_home: Path | None = None):
    # Set the latest block hash in the json file
    set_latest_block(block_hash=h, storage_home=storage_home)


def get_latest_block_height(storage_home: Path | None = None):
    # Get the latest block height from the json file
    latest_block = _load_latest_block(storage_home)
    return latest_block.get("height")


def get_latest_block_nanos(storage_home: Path | None = None):
    latest_block = _load_latest_block(storage_home)
    return int(latest_block.get("nanos", 0) or 0)


def set_latest_block_height(h, storage_home: Path | None = None):
    # Set the latest block height in the json file
    set_latest_block(height=h, storage_home=storage_home)


def set_latest_block_nanos(nanos, storage_home: Path | None = None):
    set_latest_block(nanos=nanos, storage_home=storage_home)
