import json

from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import Event, EventAttribute
from cometbft.abci.v1beta3.types_pb2 import ExecTxResult, ResponseFinalizeBlock
from xian.constants import Constants as c
from xian.services.bds.payloads import BdsBlockPayload, BdsTransactionPayload
from xian.utils.block import (
    convert_cometbft_time_to_datetime,
    get_latest_block_hash,
    get_nanotime_from_block_time,
)
from xian.utils.encoding import (
    convert_binary_to_hex,
    decode_transaction_bytes,
    encode_abci_json,
    hash_bytes,
)
from xian.utils.hash import hash_from_rewards, hash_list

STATE_CHANGE_TRANSLATION_TABLE = str.maketrans({".": "_", ":": "__"})


def _error_tx_result(message: str) -> ExecTxResult:
    logger.error(message)
    return ExecTxResult(
        code=c.ErrorCode,
        data=json.dumps({"error": message}).encode(),
        gas_used=0,
    )


async def finalize_block(self, req) -> ResponseFinalizeBlock:
    nanos = get_nanotime_from_block_time(req.time)
    hash = convert_binary_to_hex(req.hash)
    block_datetime = convert_cometbft_time_to_datetime(nanos)
    height = req.height
    tx_results = []
    reward_writes = []
    bds_transactions = []
    latest_block_hash = get_latest_block_hash()
    self.fingerprint_hashes.append(latest_block_hash.hex())

    self.current_block_meta = {
        "nanos": nanos,
        "height": height,
        "hash": hash,
        "chain_id": self.chain_id,
    }
    self.tx_processor.reset_block_cache()

    decoded_entries = []
    with self.profiler.scope("finalize_decode", block_scoped=True):
        for tx_bytes in req.txs:
            try:
                tx, _ = decode_transaction_bytes(tx_bytes)
            except Exception as e:
                decoded_entries.append(
                    {
                        "tx_bytes": tx_bytes,
                        "error": _error_tx_result(
                            f"Error decoding transaction: {e}"
                        ),
                    }
                )
                continue

            # Attach metadata to the transaction
            tx["b_meta"] = self.current_block_meta
            decoded_entries.append({"tx": tx, "tx_bytes": tx_bytes})

    decoded_txs = [entry["tx"] for entry in decoded_entries if "tx" in entry]
    processed_results = None

    with self.profiler.scope("finalize_parallel", block_scoped=True):
        parallel_execution = self.parallel_block_executor.execute(
            txs=decoded_txs,
            tx_processor=self.tx_processor,
            enabled_fees=self.enable_tx_fee,
            rewards_handler=self.rewards_handler,
        )
    if parallel_execution is not None:
        processed_results, parallel_stats = parallel_execution
        self.profiler.set_block_metadata(
            parallel_enabled=True,
            parallel_worker_count=parallel_stats.worker_count,
            parallel_planned_stage_count=parallel_stats.planned_stage_count,
            parallel_planned_parallelizable_transactions=(
                parallel_stats.planned_parallelizable_transactions
            ),
            parallel_speculative_accepted=parallel_stats.speculative_accepted,
            parallel_serial_fallbacks=parallel_stats.serial_fallbacks,
        )
        logger.info(
            "Parallel block execution accepted "
            f"{parallel_stats.speculative_accepted}/{len(decoded_txs)} "
            f"speculative results with {parallel_stats.serial_fallbacks} "
            f"serial fallbacks across {parallel_stats.planned_stage_count} stages"
        )

    processed_iter = iter(processed_results or [])

    with self.profiler.scope("finalize_tx_loop", block_scoped=True):
        for block_tx_index, entry in enumerate(decoded_entries):
            if "error" in entry:
                tx_results.append(entry["error"])
                continue

            tx = entry["tx"]
            tx_bytes = entry["tx_bytes"]
            try:
                if processed_results is not None:
                    result = next(processed_iter)
                else:
                    result = self.tx_processor.process_tx(
                        tx,
                        enabled_fees=self.enable_tx_fee,
                        rewards_handler=self.rewards_handler,
                        track_access=False,
                    )
            except Exception as e:
                tx_results.append(_error_tx_result(f"Error processing tx: {e}"))
                continue

            tx_result = result.get("tx_result")
            if tx_result is None:
                tx_results.append(
                    _error_tx_result(
                        "Transaction processor returned no tx_result"
                    )
                )
                continue

            self.nonce_storage.set_nonce_by_tx(tx)
            tx_hash = tx_result.get("hash")
            if not tx_hash:
                tx_results.append(
                    _error_tx_result(
                        "Transaction processor returned no tx hash"
                    )
                )
                continue
            self.fingerprint_hashes.append(tx_hash)
            parsed_tx_result = encode_abci_json(tx_result)
            if self.transaction_trace_logging:
                logger.debug(f"Parsed tx result: {parsed_tx_result.decode()}")

            tx_events = []

            if tx_result["status"] == 0:
                state_changes = []
                for state in tx_result["state"]:
                    state_key = state["key"].translate(
                        STATE_CHANGE_TRANSLATION_TABLE
                    )
                    state_value = str(state["value"])
                    state_changes.append(
                        EventAttribute(key=state_key, value=state_value)
                    )
                if state_changes:
                    tx_events.append(
                        Event(type="StateChange", attributes=state_changes)
                    )

                for contract_event in tx_result.get("events", []):
                    attrs = [
                        EventAttribute(
                            key="contract",
                            value=str(contract_event.get("contract", "")),
                            index=True,
                        ),
                        EventAttribute(
                            key="signer",
                            value=str(contract_event.get("signer", "")),
                            index=True,
                        ),
                        EventAttribute(
                            key="caller",
                            value=str(contract_event.get("caller", "")),
                            index=True,
                        ),
                    ]
                    for key, value in contract_event.get(
                        "data_indexed", {}
                    ).items():
                        attrs.append(
                            EventAttribute(
                                key=str(key),
                                value=str(value),
                                index=True,
                            )
                        )
                    for key, value in contract_event.get("data", {}).items():
                        attrs.append(
                            EventAttribute(
                                key=str(key),
                                value=str(value),
                                index=False,
                            )
                        )
                    tx_events.append(
                        Event(
                            type=str(
                                contract_event.get("event", "ContractEvent")
                            ),
                            attributes=attrs,
                        )
                    )

            tx_results.append(
                ExecTxResult(
                    code=tx_result["status"],
                    data=parsed_tx_result,
                    gas_used=0,
                    events=tx_events,
                )
            )

            # Save data to BDS - Add tx data to batch
            if self.block_service_mode:
                cometbft_hash = hash_bytes(tx_bytes).upper()
                tx_result["hash"] = cometbft_hash
                bds_transactions.append(
                    BdsTransactionPayload(
                        tx_index=block_tx_index,
                        envelope={
                            key: value
                            for key, value in tx.items()
                            if key != "b_meta"
                        },
                        payload=tx["payload"],
                        tx_result=tx_result,
                    )
                )

    if self.static_rewards:
        with self.profiler.scope("finalize_rewards", block_scoped=True):
            try:
                reward_writes.append(
                    self.rewards_handler.distribute_static_rewards(
                        master_reward=self.static_rewards_amount_validators,
                        foundation_reward=self.static_rewards_amount_foundation,
                    )
                )
            except Exception as e:
                logger.error(f"STATIC REWARD ERROR: {e} for block")

    with self.profiler.scope("finalize_fingerprint", block_scoped=True):
        reward_hash = hash_from_rewards(reward_writes)
        validator_updates = self.validator_handler.build_validator_updates(
            height
        )

        self.fingerprint_hashes.append(reward_hash)

        # Apply any state patches for this block and include hash in fingerprint
        state_patch_applied = False
        patch_hash = None
        applied_patches = []
        if hasattr(self, "state_patch_manager"):
            patch_hash, applied_patches = (
                self.state_patch_manager.apply_patches_for_block(height, nanos)
            )

            # If patches were applied, include the hash in fingerprint hashes
            if patch_hash:
                self.fingerprint_hashes.append(patch_hash)
                state_patch_applied = True
                logger.info(
                    f"Added state patch hash to block fingerprint: {patch_hash}"
                )

        # No transactions and no state patches = no change to ABCI state, use previous block hash.
        # Otherwise, compute a new hash from the fingerprint hashes.
        self.merkle_root_hash = (
            latest_block_hash
            if (len(req.txs) == 0 and not state_patch_applied)
            else hash_list(self.fingerprint_hashes)
        )

        if self.block_service_mode:
            with self.profiler.scope("finalize_bds_enqueue"):
                await self.bds.enqueue_block(
                    BdsBlockPayload(
                        block_meta=self.current_block_meta.copy(),
                        block_time=block_datetime,
                        app_hash=self.merkle_root_hash.hex().upper(),
                        transactions=bds_transactions,
                        state_patches=applied_patches,
                        state_patch_hash=patch_hash,
                    )
                )

    self.profiler.set_block_metadata(
        decoded_tx_count=len(decoded_txs),
        error_tx_count=sum(1 for entry in decoded_entries if "error" in entry),
        finalized_tx_result_count=len(tx_results),
        static_reward_writes=len(reward_writes),
    )

    return ResponseFinalizeBlock(
        validator_updates=validator_updates,
        tx_results=tx_results,
        app_hash=self.merkle_root_hash,
    )
