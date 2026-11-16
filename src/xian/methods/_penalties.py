"""Advance one bounded batch of the chain's pending validator penalties."""

from loguru import logger

from xian.app_logging import build_log_fields

SYSTEM_PENALTY_SENDER = "__penalty_driver__"


def maybe_process_pending_slashes(self, *, height: int) -> bool:
    """Use one shared contract-enforced work budget, even with many queued jobs.

    The head and every job cursor are consensus state. No local queue or
    registry scan is needed, so restart/replay resumes the committed work.
    """
    if self.client.raw_driver.get("validator_penalties.pending_head") is None:
        return False
    tx = {
        "payload": {
            "sender": SYSTEM_PENALTY_SENDER,
            "contract": "validator_penalties",
            "function": "process_pending_slashes",
            "kwargs": {},
            "chi_supplied": 0,
        },
        "metadata": {"signature": f"validator-penalty-batch:{height}"},
        "b_meta": self.current_block_meta,
    }
    result = self.tx_processor.process_system_tx(tx).get("tx_result") or {}
    applied = result.get("status") == 0
    logger.bind(
        **build_log_fields(
            stage="finalize_penalty_batch",
            block_height=height,
            block_hash=self.current_block_meta["hash"],
            status=result.get("status"),
        )
    ).log("INFO" if applied else "WARNING", "Processed validator penalty batch")
    return applied and len(result.get("state", [])) > 0
