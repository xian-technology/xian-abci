import hashlib
import json

from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import Event, EventAttribute
from cometbft.abci.v1beta3.types_pb2 import ExecTxResult, ResponseFinalizeBlock
from xian.app_logging import build_log_fields
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
from xian.utils.tx import (
    SequentialNonceTracker,
    validate_consensus_transaction,
)

STATE_CHANGE_TRANSLATION_TABLE = str.maketrans({".": "_", ":": "__"})
SYSTEM_REBALANCE_SENDER = "__validator_epoch_driver__"
SYSTEM_EVIDENCE_SENDER = "__evidence_penalty_driver__"
MISBEHAVIOR_TYPE_NAMES = {
    1: "DUPLICATE_VOTE",
    2: "LIGHT_CLIENT_ATTACK",
}


def _error_tx_result(message: str, **log_context) -> ExecTxResult:
    logger.bind(**build_log_fields(**log_context)).error(message)
    return ExecTxResult(
        code=c.ErrorCode,
        data=json.dumps({"error": message}).encode(),
        gas_used=0,
    )


def _safe_positive_int(value, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    if normalized <= 0:
        return default
    return normalized


def _validator_consensus_address(pubkey_hex: str) -> bytes | None:
    try:
        return hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()[:20]
    except ValueError:
        return None


def _resolve_misbehaving_validator_key(self, validator_address: bytes) -> str | None:
    if not validator_address:
        return None
    known_validators = self.client.raw_driver.get(
        "masternodes.validator_registry",
        save=False,
    ) or self.client.raw_driver.get("masternodes.nodes", save=False) or []
    for validator_key in known_validators:
        consensus_address = _validator_consensus_address(validator_key)
        if consensus_address == validator_address:
            return validator_key
    return None


def _misbehavior_type_name(raw_type: int) -> str | None:
    return MISBEHAVIOR_TYPE_NAMES.get(int(raw_type))


def _misbehavior_evidence_id(misbehavior, validator_key: str) -> str:
    evidence_time = getattr(misbehavior, "time", None)
    seconds = getattr(evidence_time, "seconds", 0)
    nanos = getattr(evidence_time, "nanos", 0)
    validator_address = bytes(getattr(misbehavior.validator, "address", b""))
    raw_evidence_id = (
        f"{_misbehavior_type_name(misbehavior.type)}:{validator_key}:"
        f"{validator_address.hex()}:{misbehavior.height}:"
        f"{seconds}:{nanos}:{misbehavior.total_voting_power}"
    )
    return hashlib.sha256(raw_evidence_id.encode("utf-8")).hexdigest()


def _maybe_apply_evidence_penalties(self, req, *, height: int):
    misbehavior_entries = list(getattr(req, "misbehavior", []) or [])
    if len(misbehavior_entries) == 0:
        return False

    any_applied = False
    for misbehavior in misbehavior_entries:
        infraction_type = _misbehavior_type_name(misbehavior.type)
        if infraction_type is None:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={"misbehavior_type": int(misbehavior.type)},
                )
            ).warning("Ignoring unsupported validator misbehavior type")
            continue

        validator_key = _resolve_misbehaving_validator_key(
            self,
            bytes(misbehavior.validator.address),
        )
        if validator_key is None:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_address": bytes(
                            misbehavior.validator.address
                        ).hex(),
                    },
                )
            ).warning("Could not resolve validator key for misbehavior evidence")
            continue

        evidence_id = _misbehavior_evidence_id(misbehavior, validator_key)
        evidence_tx = {
            "payload": {
                "sender": SYSTEM_EVIDENCE_SENDER,
                "contract": "masternodes",
                "function": "apply_evidence_penalty",
                "kwargs": {
                    "member": validator_key,
                    "infraction_type": infraction_type,
                    "evidence_id": evidence_id,
                    "evidence_height": misbehavior.height,
                },
                "stamps_supplied": 0,
            },
            "metadata": {
                "signature": f"evidence-penalty:{evidence_id}",
            },
            "b_meta": self.current_block_meta,
        }
        result = self.tx_processor.process_tx(
            evidence_tx,
            enabled_fees=False,
            rewards_handler=None,
            track_access=False,
        )
        tx_result = result.get("tx_result")
        if tx_result is None or tx_result.get("status") != 0:
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    status=(tx_result or {}).get("status"),
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_key": validator_key,
                    },
                )
            ).warning("Evidence penalty did not apply")
            continue

        state_write_count = len(tx_result.get("state", []))
        if state_write_count > 0:
            self.fingerprint_hashes.append(hash_bytes(encode_abci_json(tx_result)))
            any_applied = True
            logger.bind(
                **build_log_fields(
                    stage="finalize_evidence",
                    block_height=height,
                    block_hash=self.current_block_meta["hash"],
                    extra={
                        "misbehavior_type": infraction_type,
                        "validator_key": validator_key,
                        "state_write_count": state_write_count,
                    },
                )
            ).info("Applied validator evidence penalty")

    return any_applied


def _maybe_run_validator_epoch_rebalance(self, *, height: int):
    driver = self.client.raw_driver
    selection_mode = driver.get("masternodes.config:selection_mode", save=False)
    if selection_mode in (None, "manual"):
        return None, False

    rebalance_interval = _safe_positive_int(
        driver.get("masternodes.config:rebalance_interval", save=False),
        1,
    )
    current_epoch = height // rebalance_interval
    last_rebalance_epoch = driver.get(
        "masternodes.last_rebalance_epoch",
        save=False,
    )
    if last_rebalance_epoch is not None and current_epoch <= last_rebalance_epoch:
        return None, False

    rebalance_tx = {
        "payload": {
            "sender": SYSTEM_REBALANCE_SENDER,
            "contract": "masternodes",
            "function": "rebalance",
            "kwargs": {},
            "stamps_supplied": 0,
        },
        "metadata": {
            "signature": f"validator-epoch-rebalance:{height}",
        },
        "b_meta": self.current_block_meta,
    }
    result = self.tx_processor.process_tx(
        rebalance_tx,
        enabled_fees=False,
        rewards_handler=None,
        track_access=False,
    )
    tx_result = result.get("tx_result")
    if tx_result is None or tx_result.get("status") != 0:
        logger.bind(
            **build_log_fields(
                stage="finalize_epoch_rebalance",
                block_height=height,
                block_hash=self.current_block_meta["hash"],
                status=(tx_result or {}).get("status"),
            )
        ).warning("Automatic epoch rebalance did not apply")
        return tx_result, False

    self.fingerprint_hashes.append(hash_bytes(encode_abci_json(tx_result)))
    logger.bind(
        **build_log_fields(
            stage="finalize_epoch_rebalance",
            block_height=height,
            block_hash=self.current_block_meta["hash"],
            extra={
                "state_write_count": len(tx_result.get("state", [])),
            },
        )
    ).info("Applied automatic validator epoch rebalance")
    return tx_result, len(tx_result.get("state", [])) > 0


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
    logger.bind(
        **build_log_fields(
            stage="finalize_start",
            block_height=height,
            block_hash=hash,
            extra={"tx_count": len(req.txs)},
        )
    ).info("Finalizing block")

    decoded_entries = []
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)
    with self.profiler.scope("finalize_decode", block_scoped=True):
        for tx_bytes in req.txs:
            try:
                tx, _ = decode_transaction_bytes(tx_bytes)
            except Exception as e:
                decoded_entries.append(
                    {
                        "tx_bytes": tx_bytes,
                        "error": _error_tx_result(
                            f"Error decoding transaction: {e}",
                            stage="finalize_decode",
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=hash,
                        ),
                    }
                )
                continue

            try:
                validate_consensus_transaction(
                    tx,
                    chain_id=self.chain_id,
                    nonce_tracker=nonce_tracker,
                )
            except Exception as e:
                decoded_entries.append(
                    {
                        "tx_bytes": tx_bytes,
                        "error": _error_tx_result(
                            f"Invalid transaction in block: {e}",
                            stage="finalize_decode",
                            tx=tx,
                            raw_tx=tx_bytes,
                            block_height=height,
                            block_hash=hash,
                        ),
                    }
                )
                continue
            # Attach block metadata only after consensus validation, since it
            # is not part of the canonical signed transaction shape.
            tx["b_meta"] = self.current_block_meta
            decoded_entries.append({"tx": tx, "tx_bytes": tx_bytes})

    decoded_txs = [entry["tx"] for entry in decoded_entries if "tx" in entry]
    logger.bind(
        **build_log_fields(
            stage="finalize_decode",
            block_height=height,
            block_hash=hash,
            extra={
                "decoded_tx_count": len(decoded_txs),
                "rejected_tx_count": sum(
                    1 for entry in decoded_entries if "error" in entry
                ),
            },
        )
    ).info("Finished block transaction validation")
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
        logger.bind(
            **build_log_fields(
                stage="finalize_parallel",
                block_height=height,
                block_hash=hash,
                extra={
                    "speculative_accepted": (
                        parallel_stats.speculative_accepted
                    ),
                    "decoded_tx_count": len(decoded_txs),
                    "serial_fallbacks": parallel_stats.serial_fallbacks,
                    "planned_stage_count": (parallel_stats.planned_stage_count),
                    "worker_count": parallel_stats.worker_count,
                },
            )
        ).info("Parallel block execution summary")

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
                tx_results.append(
                    _error_tx_result(
                        f"Error processing tx: {e}",
                        stage="finalize_tx_loop",
                        tx=tx,
                        raw_tx=tx_bytes,
                        block_height=height,
                        block_hash=hash,
                        tx_index=block_tx_index,
                    )
                )
                continue

            tx_result = result.get("tx_result")
            if tx_result is None:
                tx_results.append(
                    _error_tx_result(
                        "Transaction processor returned no tx_result",
                        stage="finalize_tx_loop",
                        tx=tx,
                        raw_tx=tx_bytes,
                        block_height=height,
                        block_hash=hash,
                        tx_index=block_tx_index,
                    )
                )
                continue

            self.nonce_storage.set_nonce_by_tx(tx)
            tx_hash = tx_result.get("hash")
            if not tx_hash:
                tx_results.append(
                    _error_tx_result(
                        "Transaction processor returned no tx hash",
                        stage="finalize_tx_loop",
                        tx=tx,
                        raw_tx=tx_bytes,
                        block_height=height,
                        block_hash=hash,
                        tx_index=block_tx_index,
                    )
                )
                continue
            self.fingerprint_hashes.append(tx_hash)
            parsed_tx_result = encode_abci_json(tx_result)
            if self.transaction_trace_debug_logging:
                logger.bind(
                    **build_log_fields(
                        stage="finalize_tx_result",
                        tx=tx,
                        tx_hash=tx_hash,
                        block_height=height,
                        block_hash=hash,
                        tx_index=block_tx_index,
                        status=tx_result["status"],
                        extra={
                            "stamps_used": tx_result["stamps_used"],
                            "state_write_count": len(tx_result["state"]),
                            "event_count": len(tx_result.get("events", [])),
                        },
                    )
                ).debug("Finalized transaction result")
            if self.transaction_trace_full_logging:
                logger.bind(
                    **build_log_fields(
                        stage="finalize_tx_result",
                        tx=tx,
                        tx_hash=tx_hash,
                        block_height=height,
                        block_hash=hash,
                        tx_index=block_tx_index,
                        status=tx_result["status"],
                        extra={
                            "payload_bytes": len(parsed_tx_result),
                        },
                    )
                ).trace(parsed_tx_result.decode())

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

    evidence_penalty_applied = False
    with self.profiler.scope("finalize_evidence", block_scoped=True):
        evidence_penalty_applied = _maybe_apply_evidence_penalties(
            self,
            req,
            height=height,
        )

    automatic_rebalance_applied = False
    with self.profiler.scope("finalize_epoch_rebalance", block_scoped=True):
        _, automatic_rebalance_applied = _maybe_run_validator_epoch_rebalance(
            self,
            height=height,
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
                logger.bind(
                    **build_log_fields(
                        stage="finalize_rewards",
                        block_height=height,
                        block_hash=hash,
                        extra={"error_type": type(e).__name__},
                    )
                ).exception("Static reward distribution failed for block")

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
                self.state_patch_manager.apply_patches_for_block(
                    height,
                    nanos,
                    block_hash=hash,
                )
            )

            # If patches were applied, include the hash in fingerprint hashes
            if patch_hash:
                self.fingerprint_hashes.append(patch_hash)
                state_patch_applied = True
                logger.bind(
                    **build_log_fields(
                        stage="finalize_fingerprint",
                        block_height=height,
                        block_hash=hash,
                        extra={"patch_hash": patch_hash},
                    )
                ).info("Added state patch hash to block fingerprint")

        # No transactions and no state patches = no change to ABCI state, use previous block hash.
        # Otherwise, compute a new hash from the fingerprint hashes.
        self.merkle_root_hash = (
            latest_block_hash
            if (
                len(req.txs) == 0
                and not state_patch_applied
                and not evidence_penalty_applied
                and not automatic_rebalance_applied
            )
            else hash_list(self.fingerprint_hashes)
        )

        if self.block_service_mode:
            with self.profiler.scope("finalize_bds_enqueue"):
                try:
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
                except Exception as exc:
                    logger.bind(
                        **build_log_fields(
                            stage="finalize_bds_enqueue",
                            block_height=height,
                            block_hash=hash,
                            extra={"error_type": type(exc).__name__},
                        )
                    ).exception("BDS enqueue failed for block")

    self.profiler.set_block_metadata(
        decoded_tx_count=len(decoded_txs),
        error_tx_count=sum(1 for entry in decoded_entries if "error" in entry),
        finalized_tx_result_count=len(tx_results),
        static_reward_writes=len(reward_writes),
    )
    logger.bind(
        **build_log_fields(
            stage="finalize_complete",
            block_height=height,
            block_hash=hash,
            extra={
                "decoded_tx_count": len(decoded_txs),
                "rejected_tx_count": sum(
                    1 for entry in decoded_entries if "error" in entry
                ),
                "finalized_tx_result_count": len(tx_results),
                "reward_write_count": len(reward_writes),
                "app_hash": self.merkle_root_hash.hex().upper(),
            },
        )
    ).info("Completed block finalization")

    return ResponseFinalizeBlock(
        validator_updates=validator_updates,
        tx_results=tx_results,
        app_hash=self.merkle_root_hash,
    )
