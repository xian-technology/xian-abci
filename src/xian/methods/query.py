import json

from contracting.compilation import parser
from loguru import logger
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import Encoder

from cometbft.abci.v1beta1.types_pb2 import ResponseQuery
from xian.constants import Constants as c
from xian.utils.encoding import encode_str


async def query(self, req) -> ResponseQuery:
    """
    Query the application state
    Request Ex. http://localhost:26657/abci_query?path="path"
    (Yes you need to quote the path)
    """

    logger.debug(req.path)
    path_parts = [part for part in req.path.split("/") if part]
    key = path_parts[1] if len(path_parts) > 1 else ""
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

        # http://localhost:26657/abci_query?path="/ping"
        elif path_parts[0] == "ping":
            result = {"status": "online"}

        # http://localhost:26657/abci_query?path="/simulate_tx/<encoded_payload>"
        elif path_parts[0] == "simulate_tx":
            raw_payload = path_parts[1]
            result = self.simulator.simulate_encoded_transaction(raw_payload)

        # Blockchain Data Service
        elif self.block_service_mode:
            if not hasattr(self, "bds"):
                raise RuntimeError("BDS service is not initialized")

            limit = 100
            offset = 0

            params = dict()
            for path in path_parts:
                if "=" in path:
                    param_list = path.split("=")
                    params[param_list[0]] = param_list[1]

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

            # http://localhost:26657/abci_query?path="/keys/currency.balances"
            if path_parts[0] == "keys":
                list_of_keys = self.client.raw_driver.keys(path_parts[1])
                result = [key.split(":")[1] for key in list_of_keys]
                key = path_parts[1]

            # http://localhost:26657/abci_query?path="/blocks/limit=10/offset=20"
            elif path_parts[0] == "blocks":
                result = await self.bds.get_blocks(limit, offset)

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
                    path_parts[1], path_parts[2], limit, offset
                )

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

            # http://localhost:26657/abci_query?path="/contracts/limit=10/offset=20"
            elif path_parts[0] == "contracts":
                result = await self.bds.get_contracts(limit, offset)

        else:
            error = f"Unknown query path: {path_parts[0]}"
            logger.error(error)
            return ResponseQuery(
                code=c.ErrorCode, value=b"\x00", info=None, log=error
            )

        if result is None:
            v = None
            type_of_data = None
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
        elif isinstance(result, dict) or isinstance(result, list):
            v = encode_str(json.dumps(result, cls=Encoder))
            type_of_data = "str"
        else:
            v = encode_str(str(result))
            type_of_data = "str"

    except Exception as err:
        logger.error(err)
        return ResponseQuery(code=c.ErrorCode)

    return ResponseQuery(
        code=c.OkCode, value=v, info=type_of_data, key=encode_str(key)
    )
