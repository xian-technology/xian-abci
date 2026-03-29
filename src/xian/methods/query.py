from contracting.compilation import parser
from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal

from cometbft.abci.v1beta1.types_pb2 import ResponseQuery
from xian.constants import Constants as c
from xian.utils.block import get_latest_block_height
from xian.utils.encoding import encode_abci_json, encode_str


def _query_params(path_parts: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for path in path_parts:
        if "=" not in path:
            continue
        key, value = path.split("=", 1)
        params[key] = value
    return params


def _submission_iso(value) -> str | None:
    if isinstance(value, dict):
        time_value = value.get("__time__")
        if isinstance(time_value, (list, tuple)) and len(time_value) >= 6:
            year, month, day, hour, minute, second = time_value[:6]
            return (
                f"{int(year):04d}-{int(month):02d}-{int(day):02d}T"
                f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}Z"
            )
    return None


def _sort_contract_records(
    records: list[dict],
    *,
    sort_key: str,
    descending: bool,
) -> list[dict]:
    if sort_key == "name":
        return sorted(
            records,
            key=lambda record: (
                str(record.get("name", "")).lower(),
                str(record.get("submitted_at") or ""),
            ),
            reverse=descending,
        )

    return sorted(
        records,
        key=lambda record: (
            str(record.get("submitted_at") or ""),
            str(record.get("name", "")).lower(),
        ),
        reverse=descending,
    )


async def query(self, req) -> ResponseQuery:
    """
    Query the application state
    Request Ex. http://localhost:26657/abci_query?path="path"
    (Yes you need to quote the path)
    """

    logger.debug(req.path)
    path_parts = [part for part in req.path.split("/") if part]
    key = path_parts[1] if len(path_parts) > 1 else ""
    params = _query_params(path_parts)
    result = None
    try:
        # http://localhost:26657/abci_query?path="/get/currency.balances:c93dee52d7dc6cc43af44007c3b1dae5b730ccf18a9e6fb43521f8e4064561e6"
        if path_parts and path_parts[0] == "get":
            result = self.client.raw_driver.get(path_parts[1])

        # http://localhost:26657/abci_query?path="/health"
        elif path_parts[0] == "health":
            result = "OK"
        # http://localhost:26657/abci_query?path="/get_next_nonce/ddd326fddb5d1677595311f298b744a4e9f415b577ac179a6afbf38483dc0791"
        elif path_parts[0] == "get_next_nonce":
            result = self.nonce_storage.get_next_nonce(path_parts[1])

        # http://localhost:26657/abci_query?path="/contract/con_some_contract"
        elif path_parts[0] == "contract":
            result = self.client.raw_driver.get_contract_source(
                path_parts[1]
            ) or self.client.raw_driver.get_contract(path_parts[1])

        # http://localhost:26657/abci_query?path="/contract_source/con_some_contract"
        elif path_parts[0] == "contract_source":
            result = self.client.raw_driver.get_contract_source(path_parts[1])

        # http://localhost:26657/abci_query?path="/contract_code/con_some_contract"
        elif path_parts[0] == "contract_code":
            result = self.client.raw_driver.get_contract(path_parts[1])

        # http://localhost:26657/abci_query?path="/contract_methods/con_some_contract"
        elif path_parts[0] == "contract_methods":
            contract_code = self.client.raw_driver.get_contract(path_parts[1])
            if contract_code is not None:
                funcs = parser.methods_for_contract(contract_code)
                result = {"methods": funcs}

        # http://localhost:26657/abci_query?path="/contract_vars/con_some_contract"
        elif path_parts[0] == "contract_vars":
            contract_code = self.client.raw_driver.get_contract(path_parts[1])
            if contract_code is not None:
                result = parser.variables_for_contract(contract_code)

        # http://localhost:26657/abci_query?path="/contracts/limit=50/offset=0/sort=submitted_at/order=desc"
        elif path_parts[0] == "contracts":
            limit = 100
            offset = 0
            try:
                if "limit" in params:
                    limit = max(0, min(int(params["limit"]), 1000))
            except (TypeError, ValueError):
                limit = 100
            try:
                if "offset" in params:
                    offset = max(0, int(params["offset"]))
            except (TypeError, ValueError):
                offset = 0

            sort_key = params.get("sort", "submitted_at").strip().lower()
            if sort_key not in {"submitted_at", "created_at", "name"}:
                sort_key = "submitted_at"
            if sort_key == "created_at":
                sort_key = "submitted_at"
            descending = params.get("order", "desc").strip().lower() != "asc"

            raw_driver = self.client.raw_driver
            contract_names = sorted(set(self.client.get_contracts()))
            records: list[dict] = []
            for contract_name in contract_names:
                submitted_value = raw_driver.get(
                    f"{contract_name}.__submitted__"
                )
                records.append(
                    {
                        "name": contract_name,
                        "submitted_at": _submission_iso(submitted_value),
                        "developer": raw_driver.get(
                            f"{contract_name}.__developer__"
                        ),
                        "has_source": raw_driver.get_contract_source(
                            contract_name
                        )
                        is not None,
                    }
                )

            sorted_records = _sort_contract_records(
                records,
                sort_key=sort_key,
                descending=descending,
            )
            result = {
                "items": sorted_records[offset : offset + limit],
                "total": len(sorted_records),
                "limit": limit,
                "offset": offset,
                "sort": sort_key,
                "order": "desc" if descending else "asc",
            }

        # http://localhost:26657/abci_query?path="/ping"
        elif path_parts[0] == "ping":
            result = {"status": "online"}

        # http://localhost:26657/abci_query?path="/perf_status"
        elif path_parts[0] == "perf_status":
            result = self.profiler.snapshot()

        # http://localhost:26657/abci_query?path="/simulate_tx/<encoded_payload>"
        elif path_parts[0] == "simulate_tx":
            raw_payload = path_parts[1]
            result = await self.simulator.simulate_encoded_transaction(
                raw_payload
            )

        # http://localhost:26657/abci_query?path="/state_patch_bundles"
        elif path_parts[0] == "state_patch_bundles":
            result = self.state_patch_manager.get_local_bundle_inventory()

        # http://localhost:26657/abci_query?path="/scheduled_state_patches/123"
        elif path_parts[0] == "scheduled_state_patches":
            result = self.state_patch_manager.get_scheduled_patch_inventory(
                int(path_parts[1])
            )

        # Blockchain Data Service
        elif self.block_service_mode:
            if not hasattr(self, "bds"):
                raise RuntimeError("BDS service is not initialized")

            limit = 100
            offset = 0

            if "limit" in params:
                try:
                    limit = int(params["limit"])
                    if limit < 0 or limit > 1000:  # Example range check
                        limit = 100
                except (ValueError, TypeError):
                    limit = 100

            if "offset" in params:
                try:
                    offset = int(params["offset"])
                    if offset < 0:
                        offset = 0
                except (ValueError, TypeError):
                    offset = 0

            after_id = None
            if "after_id" in params:
                try:
                    after_id = int(params["after_id"])
                    if after_id < 0:
                        after_id = 0
                except (ValueError, TypeError):
                    after_id = None

            # http://localhost:26657/abci_query?path="/keys/currency.balances"
            if path_parts[0] == "keys":
                list_of_keys = self.client.raw_driver.keys(path_parts[1])
                result = [key.split(":")[1] for key in list_of_keys]
                key = path_parts[1]

            # http://localhost:26657/abci_query?path="/blocks/limit=10/offset=20"
            elif path_parts[0] == "blocks":
                result = await self.bds.get_blocks(limit, offset)

            # http://localhost:26657/abci_query?path="/bds_status"
            elif path_parts[0] == "bds_status":
                current_height = None
                if isinstance(self.current_block_meta, dict):
                    height = self.current_block_meta.get("height")
                    if isinstance(height, int):
                        current_height = height
                if current_height is None:
                    current_height = get_latest_block_height()
                result = await self.bds.get_status(
                    current_block_height=current_height
                )

            # http://localhost:26657/abci_query?path="/bds_spool/limit=10/offset=20"
            elif path_parts[0] == "bds_spool":
                result = await self.bds.get_spool_entries(limit, offset)

            # http://localhost:26657/abci_query?path="/block/123"
            elif path_parts[0] == "block":
                result = await self.bds.get_block(int(key))

            # http://localhost:26657/abci_query?path="/block_by_hash/ABC123"
            elif path_parts[0] == "block_by_hash":
                result = await self.bds.get_block_by_hash(key)

            # http://localhost:26657/abci_query?path="/tx/ABC123"
            elif path_parts[0] == "tx":
                result = await self.bds.get_tx(key)

            # http://localhost:26657/abci_query?path="/txs_for_block/123"
            elif path_parts[0] == "txs_for_block":
                result = await self.bds.get_txs_for_block(key)

            # http://localhost:26657/abci_query?path="/txs_by_sender/<vk>/limit=10/offset=20"
            elif path_parts[0] == "txs_by_sender":
                result = await self.bds.get_txs_by_sender(key, limit, offset)

            # http://localhost:26657/abci_query?path="/txs_by_contract/<name>/limit=10/offset=20"
            elif path_parts[0] == "txs_by_contract":
                result = await self.bds.get_txs_by_contract(key, limit, offset)

            # http://localhost:26657/abci_query?path="/events_for_tx/<tx_hash>"
            elif path_parts[0] == "events_for_tx":
                result = await self.bds.get_events_for_tx(key)

            # http://localhost:26657/abci_query?path="/events/<contract>/<event>/limit=10/offset=20"
            elif path_parts[0] == "events":
                result = await self.bds.get_events(
                    path_parts[1],
                    path_parts[2],
                    limit,
                    offset,
                    after_id=after_id,
                )

            # http://localhost:26657/abci_query?path="/recent_events/limit=25/offset=0"
            elif path_parts[0] == "recent_events":
                result = {
                    "available": True,
                    "items": await self.bds.get_recent_events(limit, offset),
                    "limit": limit,
                    "offset": offset,
                }

            # http://localhost:26657/abci_query?path="/state/currency.balances"
            elif path_parts[0] == "state":
                result = await self.bds.get_state(key, limit, offset)

            # http://localhost:26657/abci_query?path="/state_history/currency.balances:ee06a34cf08bf72ce592d26d36b90c79daba2829ba9634992d034318160d49f9/limit=10/offset=20"
            elif path_parts[0] == "state_history":
                result = await self.bds.get_state_history(key, limit, offset)

            # http://localhost:26657/abci_query?path="/state_for_tx/f39b4ea880088cfae45538acb2f7fdae1e70112185a5523d1027bcf74eac3919"
            elif path_parts[0] == "state_for_tx":
                result = await self.bds.get_state_for_tx(key)

            # Block Height: http://localhost:26657/abci_query?path="/state_for_block/662"
            # Block Hash: http://localhost:26657/abci_query?path="/state_for_block/34F1A1C923D23C5C0531490E714FC56F501EDADF05B6BF68C2ED3923234E0CC4"
            elif path_parts[0] == "state_for_block":
                result = await self.bds.get_state_for_block(key)

            # http://localhost:26657/abci_query?path="/state_patches"
            elif path_parts[0] == "state_patches":
                result = await self.bds.get_state_patches(limit, offset)

            # http://localhost:26657/abci_query?path="/state_patches_for_block/123"
            elif path_parts[0] == "state_patches_for_block":
                result = await self.bds.get_state_patches_for_block(
                    int(path_parts[1])
                )

            # http://localhost:26657/abci_query?path="/state_patch/ABC123"
            elif path_parts[0] == "state_patch":
                result = await self.bds.get_state_patch_by_hash(key)

            # http://localhost:26657/abci_query?path="/state_changes_for_patch/ABC123"
            elif path_parts[0] == "state_changes_for_patch":
                result = await self.bds.get_state_changes_for_patch(key)

            # http://localhost:26657/abci_query?path="/developer_rewards/<vk>"
            elif path_parts[0] == "developer_rewards":
                result = await self.bds.get_developer_rewards(key)

        else:
            error = f"Unknown query path: {path_parts[0]}"
            logger.error(error)
            return ResponseQuery(
                code=c.ErrorCode, value=b"\x00", info=None, log=error
            )

        if result is None:
            v = None
            type_of_data = None
        elif isinstance(result, bool):
            v = encode_str("True" if result else "False")
            type_of_data = "bool"
        elif isinstance(result, str):
            v = encode_str(result)
            type_of_data = "str"
        elif isinstance(result, int):
            v = encode_str(str(result))
            type_of_data = "int"
        elif isinstance(result, float) or isinstance(
            result, ContractingDecimal
        ):
            v = encode_str(str(result))
            type_of_data = "decimal"
        elif isinstance(result, dict):
            v = encode_abci_json(result)
            type_of_data = "dict"
        elif isinstance(result, list):
            v = encode_abci_json(result)
            type_of_data = "list"
        else:
            v = encode_str(str(result))
            type_of_data = "str"

    except Exception as err:
        logger.error(err)
        return ResponseQuery(code=c.ErrorCode)

    return ResponseQuery(
        code=c.OkCode, value=v, info=type_of_data, key=encode_str(key)
    )
